#!/usr/bin/env python
#
# edna.py -- an MP3 server
#
# Copyright (C) 1999-2000 Greg Stein. All Rights Reserved.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
#
# This software is maintained by Greg and is available at:
#    http://www.lyra.org/greg/edna/
#
# Here is the CVS ID for tracking purposes:
#   $Id: edna.py,v 1.32 2001/02/21 00:32:34 gstein Exp $
#

__version__ = '0.4'

import SocketServer
import BaseHTTPServer
import ConfigParser
import sys
import string
import os
import cgi
import urllib
import socket
import re
import stat
import random
import time
import struct
import ezt

try:
  import cStringIO
  StringIO = cStringIO
except ImportError:
  import StringIO

error = __name__ + '.error'

TITLE = 'edna: Streaming MP3 Server'

# a pattern used to trim leading digits, spaces, and dashes from a song
### would be nice to get a bit fancier with the possible trimming
re_trim = re.compile('[-0-9 ]*-[ ]*(.*)')

# determine which mixin to use: prefer threading, fall back to forking.
try:
  import thread
  mixin = SocketServer.ThreadingMixIn
except ImportError:
  if not hasattr(os, 'fork'):
    print "ERROR: your platform does not support threading OR forking."
    sys.exit(1)
  mixin = SocketServer.ForkingMixIn


class Server(mixin, BaseHTTPServer.HTTPServer):
  def __init__(self, fname):
    prh = PseudoRequestHandler(fname)
    self.port = prh.config.getint('server', 'port')
    SocketServer.TCPServer.__init__(
      self,
      (prh.config.get('server', 'binding-hostname'), self.port),
      prh)

  def server_bind(self):
    # set SO_REUSEADDR (if available on this platform)
    if hasattr(socket, 'SOL_SOCKET') and hasattr(socket, 'SO_REUSEADDR'):
      self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    BaseHTTPServer.HTTPServer.server_bind(self)


class PseudoRequestHandler:
  "Hold configuration and runtime state, and spawn real request handlers."

  def __init__(self, fname):
    self.userLog = [ ] # to track server usage
    self.userIPs = { } # log unique IPs

    config = self.config = ConfigParser.ConfigParser()

    config.add_section('server')
    config.add_section('sources')
    config.add_section('acl')

    # set up some defaults for the web server.
    d = config.defaults()
    d['port'] = '8080'
    d['binding-hostname'] = ''
    d['log'] = ''
    d['template-dir'] = 'templates'
    d['template'] = 'default.ezt'

    config.read(fname)

    template_path = config.get('server', 'template-dir')
    template_file = config.get('server', 'template')
    template_path = os.path.join(os.path.dirname(fname), template_path)

    tfname = os.path.join(template_path, template_file)
    self.default_template = ezt.Template(tfname)

    tfname = os.path.join(template_path, 'style-xml.ezt')
    self.xml_template = ezt.Template(tfname)

    tfname = os.path.join(template_path, 'stats.ezt')
    self.stats_template = ezt.Template(tfname)

    self.dirs = [ ]
    dirs = [ ]
    for option in config.options('sources'):
      if option[:3] == 'dir':
        dirs.append((int(option[3:]), config.get('sources', option)))
    if not dirs:
      raise error, 'no sources'
    dirs.sort()
    for i in range(len(dirs)):
      dir = map(string.strip, string.split(dirs[i][1], '='))
      if len(dir) == 1:
        name = dir[0]
      else:
        name = dir[1]
      if not os.path.isdir(dir[0]):
        print "WARNING: a source's directory must exist"
        print "   skipping: dir%d = %s = %s" % (dirs[i][0], dir[0], name)
        continue
      if string.find(name, '/') != -1:
        print "WARNING: a source's display name cannot contain '/'"
        print "   skipping: dir%d = %s = %s" % (dirs[i][0], dir[0], name)
        continue
      self.dirs.append((dir[0], name))

    self.acls = []
    try:
      allowed = re.split(r'[\s\n,]+', config.get('acl', 'allow'))
    except ConfigParser.NoOptionError:
      allowed = []
    for addr in allowed:
      if '/' in addr:
        addr, masklen = string.split(addr, '/')
        masklen = int(masklen)
      else:
        masklen = 32
      if not re.match(r'^\d+\.\d+\.\d+\.\d+$', addr):
        addr = socket.gethostbyname(addr)
      mask = ~((1 << (32-masklen)) - 1)
      entry = (dot2int(addr), mask)
      if not entry in self.acls:
        self.acls.append(entry)

  def __call__(self, request, client_address, server):
    return EdnaRequestHandler(request, client_address, server, self)

  def log_user(self, ip, tm, url):
    if len(self.userLog) > 19:
      # delete the oldest entry
      self.userLog.pop(0)

    # append it to the queue
    self.userLog.append((ip, tm, url))

    if ip not in self.userIPs.keys():
      # add the entry for the first time
      self.userIPs[ip] = (1, tm)
    else: 
      # increment the count and add the most recent time
      count, oldTime = self.userIPs[ip]
      self.userIPs[ip] = (count + 1, tm)

  def acl_ok(self, ipaddr):
    if not self.acls:
      return 1
    ipaddr = dot2int(ipaddr)
    for allowed, mask in self.acls:
      if (ipaddr & mask) == (allowed & mask):
        return 1
    return 0


class EdnaRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):

  def __init__(self, request, client_address, server, system):
    self.system = system
    SocketServer.BaseRequestHandler.__init__(self, request, client_address,
                                             server)

  def do_GET(self):
    try:
      self._perform_GET()
    except ClientAbortedException:
      pass

  def _perform_GET(self):
    if not self.system.acl_ok(self.client_address[0]):
      self.send_error(403, 'Forbidden')
      return
    path = self.translate_path()
    if path is None:
      self.send_error(400, 'Illegal URL construction')
      return

    self.output_style = 'html'
    if len(path) >= 1:
      if path[0] == 'xml':
          path.pop(0)
          self.output_style = 'xml'

    if not path:
      subdirs = map(lambda x: (x[1], x[1]+'/', 0), self.system.dirs)
      self.display_page(TITLE, subdirs, skiprec=1)
    elif path[0] == 'stats':
      # the site statistics were requested
      self.display_stats()
    else:
      for d, name in self.system.dirs:
        if path[0] == name:
          curdir = d
          break
      else:
        self.send_error(404)
        return

      url = '/' + urllib.quote(path[0])
      for i in range(1, len(path)):
        p = path[i]
        if p == 'all.m3u' or p == 'allrecursive.m3u' or \
           p == 'shuffle.m3u' or p == 'shufflerecursive.m3u':
          # serve up a pseudo-file
          self.serve_file(p, curdir, url)
          return

        pathname = os.path.join(curdir, p)
        base, ext = os.path.splitext(p)
        if string.lower(ext) == '.m3u':
          base, ext = os.path.splitext(base)
          if extensions.has_key(string.lower(ext)):
            # something.mp3.m3u -- one of our pseudo-files
            pathname = os.path.join(curdir, base + ext)

        if not os.path.exists(pathname):
          self.send_error(404)
          return

        if os.path.isfile(pathname):
          # requested a file.
          self.serve_file(p, pathname, url)
          return

        curdir = pathname
        url = url + '/' + urllib.quote(p)

      # requested a directory.

      # ensure there is a trailing slash so that the (relative) href
      # values will work.
      if self.path[-1] != '/':
        redir = self.build_url('/' + string.join(path, '/'))
        self.redirect(redir)
        return

      pictures = []
      subdirs = []
      songs = []
      playlists = []

      thisdir = path[-1]
      thisdirlen = len(thisdir)

      for name in sort_dir(curdir):
        st_ctime = os.stat(os.path.join(curdir, name))[stat.ST_CTIME]

        base, ext = os.path.splitext(name)
        ext = string.lower(ext)
        if picture_extensions.has_key(ext):
          pictures.append((base, name, st_ctime))
          continue
        if extensions.has_key(ext):
          # if a song has a prefix that matches the directory, and something
          # exists after that prefix, then strip it. don't strip if the
          # directory is a single-letter.
          if base[:thisdirlen] == thisdir and len(base) > thisdirlen > 1:
            base = base[thisdirlen:]

            # trim a bit of stuff off of the file
            match = re_trim.match(base)
            if match:
              base = match.group(1)

          file = open(curdir + '/' + name, 'rb')
          if file:
            info = MP3Info(file)
            if info.valid:
              info.text = base
              base = info
          songs.append((base, name, st_ctime))
        elif ext == '.m3u':
          playlists.append((base, name, st_ctime))
        else:
          newdir = os.path.join(curdir, name)
          if os.path.isdir(newdir):
            subdirs.append((name, name + '/', st_ctime))
      self.display_page(TITLE, subdirs, pictures, songs, playlists)

  def display_stats(self):
    self.send_response(200)
    self.send_header("Content-Type", 'text/html')
    self.end_headers()

    data = { 'users' : [ ],
             'ips' : [ ],
             }

    user_log = self.system.userLog
    for i in range(len(user_log) - 1, -1, -1):
      d = _datablob()
      d.ip, tm, d.url = user_log[i]
      d.time = time.strftime("%B %d %I:%M:%S %p", time.localtime(tm))
      data['users'].append(d)

    ip_log = self.system.userIPs
    ips = ip_log.keys()
    ips.sort()
    for ip in ips:
      d = _datablob()
      d.ip = ip
      d.count, tm = ip_log[ip]
      d.time = time.strftime("%B %d %I:%M:%S %p", time.localtime(tm))
      data['ips'].append(d)

    self.system.stats_template.generate(self.wfile, data)

  def display_page(self, title, subdirs, pictures=[], songs=[], playlists=[],
                   skiprec=0):

    ### implement a URL-selectable style here with a cache of templates
    if self.output_style == 'html':
      template = self.system.default_template
      content_type = 'text/html'
    else: # == 'xml'
      template = self.system.xml_template
      content_type = 'text/xml'

    self.send_response(200)
    self.send_header("Content-Type", content_type)
    self.end_headers()

    data = { 'title' : title,
             'links' : self.tree_position(),
             'pictures' : [ ],
             'subdirs' : [ ],
             'songs' : [ ],
             'playlists' : [ ],
             }

    if not skiprec:
      data['display-recursive'] = 'yes'
    else:
      data['display-recursive'] = ''

    for text, href, ctime in pictures:
      d = _datablob()
      d.href = urllib.quote(href)
      d.is_new = check_new(ctime)
      data['pictures'].append(d)

    for text, href, ctime in subdirs:
      d = _datablob()
      d.href = urllib.quote(href)
      d.text = cgi.escape(text)
      d.is_new = check_new(ctime)
      data['subdirs'].append(d)

    for text, href, ctime in songs:
      d = _datablob()
      d.href = urllib.quote(href)
      if isinstance(text, MP3Info):
        d.text = cgi.escape(text.text)
        d.info = text
      else:
        d.text = cgi.escape(text)
      d.is_new = check_new(ctime)
      data['songs'].append(d)

    for text, href, ctime in playlists:
      d = _datablob()
      d.href = urllib.quote(href)
      d.text = cgi.escape(text)
      d.is_new = check_new(ctime)
      data['playlists'].append(d)

    template.generate(self.wfile, data)

  def tree_position(self):
    ### fix: if current position is HOME, then return nothing...

    treepos = '<p><a href="' + self.build_url('','') + '">HOME</a>\n'
    mypath = self.translate_path()
    last = len(mypath)
    position = ''
    for count in range(last):
      position = position + '/' + mypath[count]
      if count + 1 < last:
        treepos = treepos + '<b> / </b><a href="' + self.build_url(urllib.quote(position),'') + '">' + mypath[count] + '</a>\n'
      else:
        treepos = treepos + '<b> : </b><b>' + mypath[count] + '</b>\n'
    return treepos + '</p>'

  def make_list(self, fullpath, url, recursive, shuffle, songs=None):
    # This routine takes a string for 'fullpath' and 'url', a list for
    # 'songs' and a boolean for 'recursive' and 'shuffle'. If recursive is
    # false make_list will return a list of every file ending in '.mp3' in
    # fullpath. If recursive is true make_list will return a list of every
    # file ending in '.mp3' in fullpath and in every directory beneath
    # fullpath.
    #
    # WARNING: There is no checking for the recursive directory structures
    # which are possible in most Unixes using ln -s etc...  If you have
    # such a directory structure, make_list will continue to traverse it 
    # until it hits the inherent limit in Python for the number of functions.
    # This number is quite large. I found this out the hard way :). Learn
    # from my experience...

    if songs is None:
      songs = []

    for name in sort_dir(fullpath):
      base, ext = os.path.splitext(name)
      if extensions.has_key(string.lower(ext)):
        # add the song's URL to the list we're building
        songs.append(self.build_url(url, name) + '\n')

      # recurse down into subdirectories looking for more MP3s.
      if recursive and os.path.isdir(fullpath + '/' + name):
        songs = self.make_list(fullpath + '/' + name,
                               url + '/' + urllib.quote(name),
                               recursive, 0,	# don't shuffle subdir results
                               songs)

    # The user asked us to mix up the results.
    if shuffle:
      count = len(songs)
      for i in xrange(count):
        j = random.randrange(count)
        songs[i], songs[j] = songs[j], songs[i]

    return songs

  def open_playlist(self, fullpath):
    ### revamp this. use some ideas from Stephen Norris' alternate patch.
    dir = os.path.dirname(fullpath)
    f = open(fullpath)
    buffer = ""
    for str in f.readlines():
      if str[:5] == "http:":
        buffer = buffer + str
      else:
        str = str[:-1]
        str = os.path.normpath(os.path.join(dir, str))
        if not os.path.exists(str): continue
        found = 0
        for d, name in self.system.dirs:
          if string.lower(str[:len(d)]) == string.lower(d):
            str = name + str[len(d):]
            found = 1
        if not found: continue
        str = string.replace(str, "\\", "/")
        buffer = buffer + self.build_url("", str) + "\n"
    f.close()
    f = StringIO.StringIO(buffer)
    return f

  def serve_file(self, name, fullpath, url):
    base, ext = os.path.splitext(name)
    ext = string.lower(ext)
    if any_extensions.has_key(ext):
      # log the request of this file
      ip, port = self.client_address
      self.system.log_user(ip, time.time(), url + '/' + urllib.quote(name))

      # get the file and info for delivery
      type = any_extensions[ext]
      f = open(fullpath, 'rb')
      clen = os.fstat(f.fileno())[stat.ST_SIZE]
    elif ext != '.m3u':
      self.send_error(404)
      return
    else:
      type = 'audio/x-mpegurl'
      if name == 'all.m3u' or name == 'allrecursive.m3u' or \
         name == 'shuffle.m3u' or name == 'shufflerecursive.m3u':
        recursive = name == 'allrecursive.m3u' or name == 'shufflerecursive.m3u'
        shuffle = name == 'shuffle.m3u' or name == 'shufflerecursive.m3u'

        # generate the list of URLs to the songs
        songs = self.make_list(fullpath, url, recursive, shuffle)

        f = StringIO.StringIO(string.join(songs, ''))
        clen = len(f.getvalue())
      else:
        base, ext = os.path.splitext(base)
        if extensions.has_key(string.lower(ext)):
          f = StringIO.StringIO(self.build_url(url, base) + ext + '\n')
          clen = len(f.getvalue())
        else:
          f = self.open_playlist(fullpath)
          clen = len(f.getvalue())

    self.send_response(200)
    self.send_header("Content-Type", type)
    self.send_header("Content-Length", clen)
    self.end_headers()

    while 1:
      data = f.read(8192)
      if not data:
        break
      try:
        self.wfile.write(data)
      except socket.error:
        # it was probably closed on the other end
        break

  def build_url(self, url, file=''):
    host = self.headers.getheader('host') or self.server.server_name
    if string.find(host, ':'):
      return 'http://%s%s/%s' % (host, url, urllib.quote(file))
    return 'http://%s:%s%s/%s' % (host, self.server.server_port, url,
                                  urllib.quote(file))

  def translate_path(self):
    parts = string.split(urllib.unquote(self.path), '/')
    parts = filter(None, parts)
    while 1:
      try:
        parts.remove('.')
      except ValueError:
        break
    while 1:
      try:
        idx = parts.index('..')
      except ValueError:
        break
      if idx == 0:
        # illegal path: the '..' attempted to go above the root
        return None
      del parts[idx-1:idx+1]
    return parts

  def redirect(self, url):
    "Send a redirect to the specified URL."
    self.log_error("code 301 -- Moved")
    self.send_response(301, 'Moved')
    self.send_header('Location', url)
    self.end_headers()
    self.wfile.write(self.error_message_format %
                     {'code': 301,
                      'message': 'Moved',
                      'explain': 'Object moved permanently'})

  def log_request(self, code='-', size='-'):
    try:
      self.log_message('"%s" %s', self.path, code)
    except AttributeError:
      # sometimes, we get an error before self.path exists
      self.log_message('<unknown URL> %s', code)

  def log_message(self, format, *args):
    log = self.system.config.get('server', 'log')
    if not log:
      return

    msg = "%s [%s] %s\n" % (self.address_string(),
                                self.log_date_time_string(),
                                format % args)
    if log == '-':
      sys.stdout.write(msg)
    else:
      try:
        open(log, 'a').write(msg)
      except IOError:
        pass

  def setup(self):
    SocketServer.StreamRequestHandler.setup(self)

    # wrap the wfile with a class that will eat up "Broken pipe" errors
    self.wfile = _SocketWriter(self.wfile)

  def finish(self):
    # if the other end breaks the connection, these operations will fail
    try:
      self.wfile.close()
    except socket.error:
      pass
    try:
      self.rfile.close()
    except socket.error:
      pass

  def version_string(self):
    return BaseHTTPServer.BaseHTTPRequestHandler.version_string(self) \
           + ' edna/' + __version__


class _SocketWriter:
  "This class ignores 'Broken pipe' errors."
  def __init__(self, wfile):
    self.wfile = wfile

  def __getattr__(self, name):
    return getattr(self.wfile, name)

  def write(self, buf):
    try:
      return self.wfile.write(buf)
    except IOError, v:
      if v.errno != 32:
        # not a 'Broken pipe'... re-raise the error
        raise
      print 'NOTICE: Client closed the connection prematurely'
      raise ClientAbortedException

class ClientAbortedException(Exception):
  pass

class _datablob:
  pass

_genres = [
  "Blues", "Classic Rock", "Country", "Dance", "Disco", "Funk", "Grunge",
  "Hip-Hop", "Jazz", "Metal", "New Age", "Oldies", "Other", "Pop", "R&B",
  "Rap", "Reggae", "Rock", "Techno", "Industrial", "Alternative", "Ska",
  "Death Metal", "Pranks", "Soundtrack", "Euro-Techno", "Ambient", "Trip-Hop",
  "Vocal", "Jazz+Funk", "Fusion", "Trance", "Classical", "Instrumental",
  "Acid", "House", "Game", "Sound Clip", "Gospel", "Noise", "AlternRock",
  "Bass", "Soul", "Punk", "Space", "Meditative", "Instrumental Pop",
  "Instrumental Rock", "Ethnic", "Gothic", "Darkwave", "Techno-industrial",
  "Electronic", "Pop-Folk", "Eurodance", "Dream", "Southern Rock", "Comedy",
  "Cult", "Gangsta", "Top 40", "Christian Rap", "Pop/Funk", "Jungle",
  "Native American", "Cabaret", "New Wave", "Psychadelic", "Rave",
  "Showtunes", "Trailer", "Lo-Fi", "Tribal", "Acid Punk", "Acid Jazz",
  "Polka", "Retro", "Musical", "Rock & Roll", "Hard Rock", "Folk",
  "Folk/Rock", "National Folk", "Swing", "Fast-Fusion", "Bebob", "Latin",
  "Revival", "Celtic", "Bluegrass", "Avantegarde", "Gothic Rock",
  "Progressive Rock", "Psychedelic Rock", "Symphonic Rock", "Slow Rock",
  "Big Band", "Chorus", "Easy Listening", "Acoustic", "Humour", "Speech",
  "Chanson", "Opera", "Chamber Music", "Sonata", "Symphony", "Booty Bass",
  "Primus", "Porn Groove", "Satire", "Slow Jam", "Club", "Tango", "Samba",
  "Folklore", "Ballad", "Power Ballad", "Rythmic Soul", "Freestyle", "Duet",
  "Punk Rock", "Drum Solo", "A capella", "Euro-House", "Dance Hall", "Goa",
  "Drum & Bass", "Club House", "Hardcore", "Terror", "Indie", "BritPop",
  "NegerPunk", "Polsk Punk", "Beat", "Christian Gangsta", "Heavy Metal",
  "Black Metal", "Crossover", "Contemporary C", "Christian Rock", "Merengue",
  "Salsa", "Thrash Metal", "Anime", "JPop", "SynthPop",
  ]

_bitrates = [
  [ # MPEG-2 & 2.5
    [0,32,48,56, 64, 80, 96,112,128,144,160,176,192,224,256,None], # Layer 1
    [0, 8,16,24, 32, 40, 48, 56, 64, 80, 96,112,128,144,160,None], # Layer 2
    [0, 8,16,24, 32, 40, 48, 56, 64, 80, 96,112,128,144,160,None]  # Layer 3
    ],

  [ # MPEG-1
    [0,32,64,96,128,160,192,224,256,288,320,352,384,416,448,None], # Layer 1
    [0,32,48,56, 64, 80, 96,112,128,160,192,224,256,320,384,None], # Layer 2
    [0,32,40,48, 56, 64, 80, 96,112,128,160,192,224,256,320,None]  # Layer 3
    ]
  ]

_samplerates = [
  [ 11025, 12000,  8000, None], # MPEG-2.5
  [  None,  None,  None, None], # reserved
  [ 22050, 24000, 16000, None], # MPEG-2
  [ 44100, 48000, 32000, None], # MPEG-1
  ]

_modes = [ "stereo", "joint stereo", "dual channel", "mono" ]

_MP3_HEADER_SEEK_LIMIT = 2000

class MP3Info:
  """Extra information about an MP3 file.

  See http://www.dv.co.yu/mpgscript/mpeghdr.htm for information about the
  header and ID3v1.1.
  """

  def __init__(self, file):
    self.valid = 0

    #
    # Generic File Info
    #
    file.seek(0, 2)
    self.filesize = file.tell()

    # find a frame header, then parse it
    offset, bytes = self._find_header(file)
    if offset != -1:
      self._parse_header(bytes)
      ### offset + framelength will find another header. verify??

    if self.valid:
      self._parse_xing(file)

    # always parse out the ID3 information
    self._parse_id3(file)

    # back to the beginning!
    file.seek(0, 0)

  def _find_header(self, file):
    file.seek(0, 0)
    amount_read = 0

    # see if we get lucky with the first four bytes
    amt = 4

    while amount_read < _MP3_HEADER_SEEK_LIMIT:
      header = file.read(amt)
      if len(header) < amt:
        # awfully short file. just give up.
        return -1, None

      amount_read = amount_read + len(header)

      # on the next read, grab a lot more
      amt = 500

      # look for the sync byte
      offset = string.find(header, chr(255))
      if offset == -1:
        continue
      ### maybe verify more sync bits in next byte?

      if offset + 4 > len(header):
        more = file.read(4)
        if len(more) < 4:
          # end of file. can't find a header
          return -1, None
        amount_read = amount_read + 4
        header = header + more
      return amount_read - len(header) + offset, header[offset:offset+4]

    # couldn't find the header
    return -1, None

  def _parse_header(self, header):
    "Parse the MPEG frame header information."

    # AAAAAAAA AAABBCCD EEEEFFGH IIJJKLMM
    (bytes,) = struct.unpack('>i', header)
    mpeg_version =    (bytes >> 19) & 3  # BB   00 = MPEG2.5, 01 = res, 10 = MPEG2, 11 = MPEG1  
    layer =           (bytes >> 17) & 3  # CC   00 = res, 01 = Layer 3, 10 = Layer 2, 11 = Layer 1
    #protection_bit = (bytes >> 16) & 1  # D    0 = protected, 1 = not protected
    bitrate =         (bytes >> 12) & 15 # EEEE 0000 = free, 1111 = bad
    samplerate =      (bytes >> 10) & 3  # F    11 = res
    padding_bit =     (bytes >> 9)  & 1  # G    0 = not padded, 1 = padded
    #private_bit =    (bytes >> 8)  & 1  # H
    mode =            (bytes >> 6)  & 3  # II   00 = stereo, 01 = joint stereo, 10 = dual channel, 11 = mono
    #mode_extension = (bytes >> 4)  & 3  # JJ
    #copyright =      (bytes >> 3)  & 1  # K    00 = not copyrighted, 01 = copyrighted
    #original =       (bytes >> 2)  & 1  # L    00 = copy, 01 = original
    #emphasis =       (bytes >> 0)  & 3  # MM   00 = none, 01 = 50/15 ms, 10 = res, 11 = CCIT J.17

    if mpeg_version == 1 or layer == 0:
      # invalid frame header.
      return

    if mpeg_version == 0:
      self.mpeg_version = 2.5
    elif mpeg_version == 2: 
      self.mpeg_version = 2
    else: # mpeg_version == 3
      self.mpeg_version = 1

    self.layer = 4 - layer

    self.bitrate = _bitrates[mpeg_version & 1][self.layer - 1][bitrate]
    self.samplerate = _samplerates[mpeg_version][samplerate]

    if self.bitrate is None or self.samplerate is None:
      # invalid frame header
      return

    self.mode = _modes[mode]

    if self.layer == 1:
      framelength = ((  12 * (self.bitrate * 1000.0)/self.samplerate) + padding_bit) * 4
      samplesperframe = 384.0
    else:
      framelength =  ( 144 * (self.bitrate * 1000.0)/self.samplerate) + padding_bit
      samplesperframe = 1152.0
    self.length = (self.filesize / framelength) * (samplesperframe / self.samplerate)

    # found a valid MPEG file
    self.valid = 1

  def _parse_xing(self, file):
    """Parse the Xing-specific header.

    For variable-bitrate (VBR) MPEG files, Xing includes a header which
    can be used to approximate the (average) bitrate and the duration
    of the file.
    """
    file.seek(0, 0)
    header = file.read(128)

    i = string.find(header, 'Xing')
    if i > 0:
      (flags,) = struct.unpack('>i', header[i+4:i+8])
      if flags & 3:
        # flags says "frames" and "bytes" are present. use them.
        (frames,) = struct.unpack('>i', header[i+8:i+12])
        (bytes,) = struct.unpack('>i', header[i+12:i+16])

        if self.samplerate:
          self.length = frames * samplesperframe / self.samplerate
          self.bitrate = (bytes * 8.0 / self.length) / 1000

  def _parse_id3(self, file):
    "Parse the ID3 tag information from the file."

    try:
      file.seek(-128, 2)	# 128 bytes before the end of the file
    except IOError:
      pass
    
    if file.read(3) == 'TAG':
      self.title = strip_zero(file.read(30))
      self.artist = strip_zero(file.read(30))
      self.album = strip_zero(file.read(30))
      self.year = strip_zero(file.read(4))

      # a la ID3v1.1 w/ backwards compatiblity to ID3v1
      comment = file.read(30)
      if comment[28] == '\0':
        self.track = ord(comment[29])
        comment = comment[:28]
      else:
        self.track = None
      self.comment = strip_zero(comment)

      genre = ord(file.read(1))
      if genre < len(_genres):
        self.genre = _genres[genre]
      else:
        self.genre = None


def strip_zero(s):
  l = len(s) - 1
  while l >= 0 and (s[l] == '\0' or s[l] == ' '):
    l = l - 1
  return s[:l+1]

def _usable_file(fname):
  return fname[0] != '.'

def sort_dir(d):
  l = filter(_usable_file, os.listdir(d))
  l.sort()
  return l

def dot2int(dotaddr):
  a, b, c, d = map(int, string.split(dotaddr, '.'))
  return (a << 24) + (b << 16) + (c << 8) + (d << 0)


DAYS_NEW = 30   ### make this a config option

# return empty string or a "new since..." string
def check_new(ctime):
  if (time.time() - ctime) < DAYS_NEW * 86400:
    t = time.strftime('%B %d', time.localtime(ctime))
    return ' <i>new since %s</i>' % t
  return ''


# Extensions that WinAMP can handle: (and their MIME type if applicable)
extensions = { 
  '.mp3' : 'audio/mpeg',
  '.mid' : 'audio/mid',
  '.mp2' : 'video/mpeg',        ### is this audio or video? my Windows box
                                ### says video/mpeg
#  '.cda',                      ### what to do with .cda?
  '.it'  : 'audio/mid',
  '.xm'  : 'audio/mid',
  '.s3m' : 'audio/mid',
  '.stm' : 'audio/mid',
  '.mod' : 'audio/mid',
  '.dsm' : 'audio/mid',
  '.far' : 'audio/mid',
  '.ult' : 'audio/mid',
  '.mtm' : 'audio/mid',
  '.669' : 'audio/mid',
  '.asx' : 'video/x-ms-asf',
  '.mpg' : 'video/mpeg',
  }

# Extensions of images: (and their MIME type)
picture_extensions = { 
  '.gif' : 'image/gif',
  '.jpe' : 'image/jpeg',
  '.jpg' : 'image/jpeg',
  '.png' : 'image/png',
  }

any_extensions = {} 
any_extensions.update(extensions)
any_extensions.update(picture_extensions)

if __name__ == '__main__':
  if len(sys.argv) > 2:
    print 'USAGE: %s [config-file]' % os.path.basename(sys.argv[0])
    print '  if config-file is not specified, then edna.conf is used'
    sys.exit(1)

  if len(sys.argv) == 2:
    fname = sys.argv[1]
  else:
    fname = 'edna.conf'

  svr = Server(fname)

  print "edna: serving on port %d..." % svr.port
  svr.serve_forever()


##########################################################################
#
# TODO
#
# from Stephen Kennedy <Stephen.Kennedy@havok.com>:
#   If there is only one repository, dont display the directory chooser.
#   Allow merging of repositories into a single view.
#
# add a server admin address to the pages
#
# server-side playlist construction
# persistent playlists (Todd Rowe <trowe@soleras.com>)
# pass an MP3 (or playlist) off to a server-side player. allows remote
#   control of an MP3 jukebox/player combo.
#
# from "Daniel Carraher" <dcarrahe@biochem.umass.edu>:
#   OGG Vorbis files? anything beyond the extension?
#
# add MP3 info into the style-xml.ezt template
#          for key, val in vars(text).items():
#            if key != "valid" and val != None:
#              self.wfile.write('<%s>%s</%s>'
#                               % (cgi.escape(key), cgi.escape('%s' % (val)),
#                                  cgi.escape(key)))
#
