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
#   $Id: edna.py,v 1.17 2001/02/19 12:36:45 gstein Exp $
#

import SocketServer
import BaseHTTPServer
import ConfigParser
import sys
import string
import os
import cgi
import urllib
import StringIO
import socket
import re
import stat
import whrandom
import time

error = __name__ + '.error'

TITLE = 'Streaming MP3 Server'
FOOTER = ('<hr>'
          '<center>Powered by '
          '<a href="http://edna.sourceforge.net/">edna</a>'
          '</center>')

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
    self.userLog = [ ] # to track server usage
    self.userIPs = { } # log unique IPs
    config = ConfigParser.ConfigParser()
    # set up some defaults for the web server.
    config.add_section('server')
    d = config.defaults()
    d['port'] = '8080'
    d['binding_hostname'] = ''
    d['log'] = ''
    # Process the config file.
    self.config(fname, config)
    self.port = config.getint('server', 'port')
    SocketServer.TCPServer.__init__(
      self,
      (config.get('server', 'binding_hostname'),
       self.port),
      RequestHandler)

  def server_bind(self):
    # set SO_REUSEADDR (if available on this platform)
    if hasattr(socket, 'SOL_SOCKET') and hasattr(socket, 'SO_REUSEADDR'):
      self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # we don't need the server name/port, so skip BaseHTTPServer's work
    ### build_url() uses server.server_name and server.server_port
    SocketServer.TCPServer.server_bind(self)

  def config(self, fname, config):
    config.add_section('sources')
    config.read(fname)
    self.config = config

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
      except ConfigParser.NoSectionError:
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
        entry = (self._dot2int(addr), mask)
        if not entry in self.acls:
          self.acls.append(entry)

  def log_user(self, ip, time, fname):
    if len(self.userLog) > 19:
      # delete the oldest entry
      self.userLog.pop(0)

    # append it to the queue
    self.userLog.append((ip, time, fname))

    if ip not in self.userIPs.keys():
      # add the entry for the first time
      self.userIPs[ip] = (1, time)
    else: 
      # increment the count and add the most recent time
      count, oldTime = self.userIPs[ip]
      self.userIPs[ip] = (count + 1, time)

  def print_users(self):
    string = '<h2>Site Statistics</h2>\n<table border=\"1\">\n'
    for x in range(len(self.userLog)):
      ip, time, fname = self.userLog[-x-1]
      string = string + '<tr><td>'+ ip + '</td><td>' + time + '</td><td>' \
               + fname + "</td></tr>\n"
    string = string + '</tr></table>\n'
    string = string + '<h3>Unique IPs</h3>\n<table border=\"1\">\n'
    for x in self.userIPs.keys():
      count, time = self.userIPs[x]
      string = string + '<tr><td>' + x + '</td><td>' \
               + ("%d songs downloaded" % count) + '</td><td>' + time \
               + "</td></tr>\n"
    string = string + '</tr></table>\n'
    return string

  def acl_ok(self, ipaddr):
    if not self.acls:
      return 1
    ipaddr = self._dot2int(ipaddr)
    for allowed, mask in self.acls:
      if (ipaddr & mask) == (allowed & mask):
        return 1
    return 0

  def _dot2int(self, dotaddr):
    a, b, c, d = map(int, string.split(dotaddr, '.'))
    return (a << 24) + (b << 16) + (c << 8) + (d << 0)


class RequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):

  def do_GET(self):
    try:
      self._perform_GET()
    except ClientAbortedException:
      pass

  def _perform_GET(self):
    if not self.server.acl_ok(self.client_address[0]):
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
      subdirs = map(lambda x: (x[1], x[1]+'/'), self.server.dirs)
      self.display_page(TITLE, subdirs, skiprec=1)
    elif path[0] == 'stats':
      # the site statistics were requested
      self.send_response(200)
      self.send_header("Content-Type", "text/html")
      self.end_headers()
      self.wfile.write(self.server.print_users())
      return
    else:
      for d, name in self.server.dirs:
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
            # something.mp3.m3u -- one of our psuedo-files
            pathname = os.path.join(curdir, base + ext)

        if not os.path.exists(pathname):
          self.send_error(404)
          return

        if os.path.isfile(pathname):
          # requested a file.
          ip, port = self.client_address
          self.server.log_user(ip,
                               time.strftime("%B %d %I:%M:%S %p",
                                             time.localtime(time.time())),
                               pathname)
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
        base, ext = os.path.splitext(name)
        ext = string.lower(ext)
        if picture_extensions.has_key(ext):
          pictures.append((base, name))
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
            if info.valid == 1:
              info.text = base
              base = info
          songs.append((base, name))
        elif ext == '.m3u':
          playlists.append((base, name))
        else:
          newdir = os.path.join(curdir, name)
          if os.path.isdir(newdir):
            subdirs.append((name, name + '/'))
      self.display_page(TITLE, subdirs, pictures, songs, playlists)

  def display_page(self, title, subdirs, pictures=[], songs=[], playlists=[], skiprec=0):
    if self.output_style == 'xml':
      self.display_xml_page(title, subdirs, pictures, songs, playlists, skiprec)
    else:
      self.display_html_page(title, subdirs, pictures, songs, playlists, skiprec)

  def display_html_page(self, title, subdirs, pictures=[], songs=[], playlists=[], skiprec=0):
    self.send_response(200)
    self.send_header("Content-Type", "text/html")
    self.end_headers()

    self.wfile.write('<html><head><title>%s</title></head>'
                     '<body><h1>%s</h1>\n'
                     % (cgi.escape(title), cgi.escape(title))
                     )

    links = self.tree_position()
    self.wfile.write(links)

    self.display_html_list('Pictures', pictures)
    self.display_html_list('Subdirectories', subdirs, skiprec)
    self.display_html_list('Songs', songs)
    self.display_html_list('Playlists', playlists)

    if not subdirs and not songs and not playlists:
      self.wfile.write('<i>Empty directory</i>\n')
    else:
      self.wfile.write(links)

    self.wfile.write('<p><a href="/stats/">Server statistics</a></p>')
    self.wfile.write(FOOTER)
    self.wfile.write('</body></html>\n')

  def tree_position(self):
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

  def display_html_list(self, title, list, skipall=0):
    if list:
      if title != 'Pictures':
        self.wfile.write('<p>%s:</p><ul>\n' % title)
      for text, href in list:
        href = urllib.quote(href)
        if isinstance(text, MP3Info):
          text = text.text
        text = cgi.escape(text)
        if title == 'Pictures':
          self.wfile.write('<img src="%s">\n' % href)
        elif title == 'Songs':
          self.wfile.write('<li><a href="%s">%s</a></li>\n' % (href, text))
          #self.wfile.write('<li>%s&nbsp;[&nbsp;<a href="%s.m3u">Stream</a>&nbsp;|&nbsp;<a href="%s">Download</a>&nbsp;]</li>\n' % (text, href, href))
        else:
          self.wfile.write('<li><a href="%s">%s</a></li>\n' % (href, text))
      self.wfile.write('</ul>\n')
      if not skipall:
        if title == 'Songs':
          self.wfile.write('<p><blockquote>'
                           '<a href="all.m3u">Play all songs</a>'
                           '<br>'
                           '<a href="shuffle.m3u">Shuffle all songs</a>'
                           '</blockquote></p>\n')
        elif title == 'Subdirectories' : 
          self.wfile.write('<p><blockquote>'
                           '<a href="allrecursive.m3u">Play all songs (recursively)</a>'

                           '<br>'
                           '<a href="shufflerecursive.m3u">Shuffle all songs (recursively)</a>'
                           '</blockquote></p>\n')

  def display_xml_page(self, title, subdirs, pictures=[], songs=[], playlists=[], skiprec=0):
    self.send_response(200)
    self.send_header("Content-Type", "text/xml")
    self.end_headers()

    self.wfile.write('<?xml version="1.0" encoding="ISO-8859-1"?>\n'
                     '<edna>\n')
    ### handle the pictures
    self.display_xml_list('subdirectory', subdirs, skiprec)
    self.display_xml_list('song', songs)
    self.display_xml_list('playlist', playlists)

    self.wfile.write('</edna>\n')

  def display_xml_list(self, title, list, skipall=0):
    if list:
      for text, href in list:
        href = urllib.quote(href)
        if title == 'song':
          href = href + '.m3u'
        ### add download links for songs? or stick with just streaming?
        if isinstance(text, MP3Info):
          self.wfile.write('<%s><href>%s</href>' % (title, href))
          for key, val in vars(text).items():
            if key != "valid" and val != None:
              self.wfile.write('<%s>%s</%s>'
                               % (cgi.escape(key), cgi.escape('%s' % (val)), cgi.escape(key)))
          self.wfile.write('</%s>\n' % (title))
        else:
          self.wfile.write('<%s><href>%s</href><text>%s</text></%s>\n'
                           % (title, href, cgi.escape(text), title))
      if not skipall:
        if title == 'song':
          self.wfile.write('<playlist><href>%s</href><text>%s</text></playlist>\n'
                           % ("all.m3u", "Play all songs"))
          self.wfile.write('<playlist><href>%s</href><text>%s</text></playlist>\n'
                           % ("shuffle.m3u", "Shuffle all songs"))
        elif title == 'subdirectory' : 
          self.wfile.write('<playlist><href>%s</href><text>%s</text></playlist>\n'
                           % ("allrecursive.m3u", "Play all songs (recursively)"))
          self.wfile.write('<playlist><href>%s</href><text>%s</text></playlist>\n'
                           % ("shufflerecursive.m3u", "Shuffle all songs (recursively)"))

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
        j = int(whrandom.random() * count)
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
        for d, name in self.server.dirs:
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
    self.log_message('"%s" %s', self.path, code)

  def log_message(self, format, *args):
    log = self.server.config.get('server', 'log')
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
  [ # MPEG-1
    [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448], # Layer 1
    [0, 32, 48, 56,  64,  80,  96, 112, 128, 160, 192, 224, 256, 320, 384], # Layer 2
    [0, 32, 40, 48,  56,  64,  80,  96, 112, 128, 160, 192, 224, 256, 320]  # Layer 3
    ],

  [ # MPEG-2 & 2.5
    [0, 32, 48, 56,  64,  80,  96, 112, 128, 144, 160, 176, 192, 224, 256], # Layer 1
    [0,  8, 16, 24,  32,  40,  48,  56,  64,  80,  96, 112, 128, 144, 160], # Layer 2
    [0,  8, 16, 24,  32,  40,  48,  56,  64,  80,  96, 112, 128, 144, 160]  # Layer 3
    ]
  ]

_samplerates = [
  [ 44100, 48000, 32000], # MPEG-1
  [ 22050, 24000, 16000], # MPEG-2
  [ 11025, 12000,  8000]  # MPEG-2.5
  ]

_modes = [ "stereo", "joint stereo", "dual channel", "mono" ]

class MP3Info:
  def __init__(self, file):
    self.valid = 0

    #
    # Generic File Info
    #
    file.seek(0, 2)
    self.filesize = file.tell()

    #
    # MPEG3 Info
    #
    file.seek(0, 0)
    header = file.read(4)  # AAAAAAAA AAABBCCD EEEEFFGH IIJJKLMM
    if len(header) == 4:
      bytes = ord(header[0])<<24 | ord(header[1])<<16 | ord(header[2])<<8 | ord(header[3])
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

      if mpeg_version == 0:
        self.mpeg_version == 2.5
      elif mpeg_version == 2: 
        self.mpeg_version = 2
      elif mpeg_version == 3:
        self.mpeg_version = 1
      else:
        self.mpeg_version = None

      self.layer = 4 - layer
      if layer == 0:
        self.layer = None
        
      try:
        self.bitrate = _bitrates[int(self.mpeg_version) - 1][self.layer - 1][bitrate]
      except IndexError:
        self.bitrate = None

      try:
        self.samplerate = _samplerates[int(round(self.mpeg_version)) - 1][samplerate]
      except IndexError:
        self.samplerate = None

      if self.bitrate and self.samplerate and self.layer:
        if self.layer == 1:
          framelength = ((  12 * (self.bitrate * 1000.0)/self.samplerate) + padding_bit) * 4
          samplesperframe = 384.0
        else:
          framelength =  ( 144 * (self.bitrate * 1000.0)/self.samplerate) + padding_bit
          samplesperframe = 1152.0
        self.length = (self.filesize / framelength) * (samplesperframe / self.samplerate)
      else:
        self.length = None

      try:
        self.mode = _modes[mode]
      except IndexError:
        self.mode = None
        
      # Xing-specific header info to properly approximate bitrate
      # and length for VBR (variable bitrate) encoded mp3s
      file.seek(0, 0)
      header = file.read(128)

      i = string.find(header, 'Xing')
      if i > 0:
        i = i + 4

        flags  = ord(header[i])<<24 | ord(header[i+1])<<16 | ord(header[i+2])<<8 | ord(header[i+3]); i = i + 4
        if flags & 0x11:
          frames = ord(header[i])<<24 | ord(header[i+1])<<16 | ord(header[i+2])<<8 | ord(header[i+3]); i = i + 4
          bytes  = ord(header[i])<<24 | ord(header[i+1])<<16 | ord(header[i+2])<<8 | ord(header[i+3]); i = i + 4
          #vbr    = ord(header[i])<<24 | ord(header[i+1])<<16 | ord(header[i+2])<<8 | ord(header[i+3]); i = i + 4

          if self.samplerate:
            self.bitrate = (bytes * 8.0 / frames) * (self.samplerate / samplesperframe) / 1000
            self.length = frames * samplesperframe / self.samplerate

    #
    # ID3 Info
    #
    try:
      file.seek(-128, 2)	# 128 bytes before the end of the file
    except IOError:
      pass
    
    if file.read(3) == 'TAG':
      self.title = string.strip(file.read(30))
      self.artist = string.strip(file.read(30))
      self.album = string.strip(file.read(30))
      self.year = string.strip(file.read(4))

      # a la ID3v1.1 w/ backwards compatiblity to ID3v1
      comment = file.read(30)
      if comment[29] == '0':
        self.track = comment[30]
        comment = comment[:28]
      else:
        self.track = None
      self.comment = string.strip(comment)

      genre = ord(file.read(1))
      if genre < len(_genres):
        self.genre = _genres[genre]
      else:
        self.genre = 'unknown'

    self.valid = 1

    file.seek(0, 0)

def _usable_file(fname):
  return fname[0] != '.'

def sort_dir(d):
  l = filter(_usable_file, os.listdir(d))
  l.sort()
  return l

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
# add ACLs
#
# server-side playlist construction
# persistent playlists (Todd Rowe <trowe@soleras.com>)
# pass an MP3 (or playlist) off to a server-side player. allows remote
#   control of an MP3 jukebox/player combo.
#
# from Ken Williams <kenw@rulespace.com>:
#   double logging of requests (one for .mp3, one for .mp3.m3u)
#
# from "Daniel Carraher" <dcarrahe@biochem.umass.edu>:
#   OGG Vorbis files? anything beyond the extension?
#

