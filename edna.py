#!/usr/bin/env python
#
# edna.py -- an MP3 server
#
# Copyright (C) 2002 Fredrik Steen <fsteen@stone.nu>. All Rights Reserved.
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
#    http://edna.sourceforge.net/
#
# Here is the CVS ID for tracking purposes:
#   $Id: edna.py,v 1.54 2002/10/09 14:32:56 st0ne Exp $
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
import MP3Info

try:
  import ogg.vorbis
  oggSupport = 'yes'
except ImportError:
  oggSupport = 'no'

try:
  import cStringIO
  StringIO = cStringIO
except ImportError:
  import StringIO

error = __name__ + '.error'


TITLE = 'Streaming MP3 Server'


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

    config = self.config = ConfigParser.ConfigParser()

    config.add_section('server')
    config.add_section('sources')
    config.add_section('acl')
    config.add_section('extra')


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

    global debug_level
    debug_level = config.getint('extra', 'debug_level')
    global DAYS_NEW
    DAYS_NEW = config.getint('extra', 'days_new')

    if debug_level == 1:
      print 'Running in debug mode'

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

    try:
      auth_pairs = re.split(r'[\s\n,]+', config.get('acl', 'auth'))
      self.auth_table = {}
      for pair in auth_pairs:
        user,passw = string.split(pair,':')
        self.auth_table[user] = passw
    except ConfigParser.NoOptionError:
      self.auth_table = {}

    self.port = config.getint('server', 'port')
    try:
        SocketServer.TCPServer.__init__(self,
            (config.get('server', 'binding-hostname'), self.port),
            EdnaRequestHandler)
    except socket.error, value:
        print "edna: bind(): %s" % str(value[1])
        raise SystemExit

  def server_bind(self):
    # set SO_REUSEADDR (if available on this platform)
    if hasattr(socket, 'SOL_SOCKET') and hasattr(socket, 'SO_REUSEADDR'):
      self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    BaseHTTPServer.HTTPServer.server_bind(self)

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

  def do_GET(self):
    try:
      self._perform_GET()
    except ClientAbortedException:
      Messages().debug_message('DEBUG --- Exception catched in "do_GET" --- ClientAbortException')

  def _perform_GET(self):

    ## verify the IP
    if not self.server.acl_ok(self.client_address[0]):
      self.send_error(403, 'Forbidden')
      return

    ## verify the Username/Password
    if self.server.auth_table:
      auth_table = self.server.auth_table
      auth=self.headers.getheader('Authorization')
      this_user, this_pass = None, None
      if auth:
          if string.lower(auth[:6]) == 'basic ':
              import base64
              [name,password] = string.split(
                  base64.decodestring(string.split(auth)[-1]), ':')
              this_user, this_pass = name, password

      if auth_table.has_key(this_user) and auth_table[this_user] == this_pass:
        Messages().debug_message('DEBUG --- Authenticated --- User: ' + this_user + ' Password: ' + this_pass)
      else:
        realm='edna'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'basic realm="%s"' % realm)
        self.end_headers()
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

    if not path and len(self.server.dirs) > 1:
      subdirs = [ ]
      for d, name in self.server.dirs:
        subdirs.append(_datablob(href=urllib.quote(name) + '/', is_new='',
                                 text=cgi.escape(name)))
      self.display_page(TITLE, subdirs, skiprec=1)
    elif path and path[0] == 'stats':
      # the site statistics were requested
      self.display_stats()
    else:
      if path:
        title = cgi.escape(path[-1])
      else:
        title = TITLE
      if len(self.server.dirs) == 1:
        url = '/'
        curdir = self.server.dirs[0][0]
      else:
        url = '/' + urllib.quote(path[0])
        for d, name in self.server.dirs:
          if path[0] == name:
            curdir = d
            path.pop(0)
            break
        else:
          self.send_error(404)
          return

      for p in path:
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
          self.serve_file(p, pathname, url, self.headers.getheader('range'))
          return

        curdir = pathname
        if url == '/':
          url = '/' + urllib.quote(p)
        else:
          url = url + '/' + urllib.quote(p)

      # requested a directory.

      # ensure there is a trailing slash so that the (relative) href
      # values will work.
      if self.path[-1] != '/':
        redir = self.build_url(self.path)
        self.redirect(redir)
        return

      pictures = []
      subdirs = []
      songs = []
      playlists = []

      if path:
        thisdir = path[-1]
      else:
        # one of the top-level virtual directories
        thisdir = ''
      thisdirlen = len(thisdir)

      for name in sort_dir(curdir):
        href = urllib.quote(name)
        is_new = check_new(os.stat(os.path.join(curdir, name))[stat.ST_CTIME])

        base, ext = os.path.splitext(name)
        ext = string.lower(ext)

        if picture_extensions.has_key(ext):
          pictures.append(_datablob(href=href, is_new=is_new))
          continue

        if ext == '.m3u':
          playlists.append(_datablob(href=href, is_new=is_new,
                                     text=cgi.escape(base)))
          continue

        fullpath = os.path.join(curdir, name)
        if extensions.has_key(ext):
          # if a song has a prefix that matches the directory, and something
          # exists after that prefix, then strip it. don't strip if the
          # directory is a single-letter.
          if len(base) > thisdirlen > 1 and base[:thisdirlen] == thisdir:
            base = base[thisdirlen:]

            # trim a bit of stuff off of the file
            match = re_trim.match(base)
            if match:
              base = match.group(1)

          d = _datablob(href=href, is_new=is_new, text=cgi.escape(base))

          if ext == '.ogg':
            if oggSupport == 'yes':
              # doesn't work yet, default.ezt calls some info.mpeg things that are only in MP3Info.py :(
              #info = OggInfo(fullpath)
              info = MP3Info.MP3Info(open(fullpath, 'rb'))
            else:
              continue
          else:
            info = MP3Info.MP3Info(open(fullpath, 'rb'))

          if hasattr(info, 'length'):
            if info.length > 3600:
              info.duration = '%d:%02d:%02d' % (int(info.length / 3600),
                                                int(info.length / 60) % 60,
                                                int(info.length) % 60)
            else:
              info.duration = '%d:%02d' % (int(info.length / 60),
                                           int(info.length) % 60)
          d.info = empty_delegator(info)

          songs.append(d)
        else:
          newdir = os.path.join(curdir, name)
          if os.path.isdir(fullpath):
            subdirs.append(_datablob(href=href + '/', is_new=is_new,
                                     text=cgi.escape(name)))

      self.display_page(title, subdirs, pictures, songs, playlists)

  def display_stats(self):
    self.send_response(200)
    self.send_header("Content-Type", 'text/html')
    self.end_headers()

    data = { 'users' : [ ],
             'ips' : [ ],
             }

    user_log = self.server.userLog
    for i in range(len(user_log) - 1, -1, -1):
      d = _datablob()
      d.ip, tm, d.url = user_log[i]
      d.unquoted_url = urllib.unquote(d.url)
      d.time = time.strftime("%B %d %I:%M:%S %p", time.localtime(tm))
      data['users'].append(d)

    ip_log = self.server.userIPs
    ips = ip_log.keys()
    ips.sort()
    for ip in ips:
      d = _datablob()
      d.ip = ip
      d.count, tm = ip_log[ip]
      d.time = time.strftime("%B %d %I:%M:%S %p", time.localtime(tm))
      data['ips'].append(d)

    self.server.stats_template.generate(self.wfile, data)

  def display_page(self, title, subdirs, pictures=[], songs=[], playlists=[],
                   skiprec=0):

    ### implement a URL-selectable style here with a cache of templates
    if self.output_style == 'html':
      template = self.server.default_template
      content_type = 'text/html'
    else: # == 'xml'
      template = self.server.xml_template
      content_type = 'text/xml'

    self.send_response(200)
    self.send_header("Content-Type", content_type)
    self.end_headers()

    data = { 'title' : title,
             'links' : self.tree_position(),
             'pictures' : pictures,
             'subdirs' : subdirs,
             'songs' : songs,
             'playlists' : playlists,
             }

    if not skiprec:
      data['display-recursive'] = 'yes'
    else:
      data['display-recursive'] = ''

    template.generate(self.wfile, data)

  def tree_position(self):
    mypath = self.translate_path()
    if not mypath:
      return ''

    url = self.build_url('')[:-1]  # lose the trailing slash

    links = [ '<a href="%s/">HOME</a>\n' % url ]

    last = len(mypath)
    for count in range(last):
      url = url + '/' + urllib.quote(mypath[count])
      text = cgi.escape(mypath[count])
      if count == last - 1:
        links.append('<b> / %s</b>' % text)
      else:
        links.append('<b> / </b><a href="%s/">%s</a>' % (url, text))

    return '<p>' + string.join(links, '\n') + '</p>'

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

  def open_playlist(self, fullpath, url):
    dirpath = os.path.dirname(fullpath)
    f = open(fullpath)

    # if the first line has 'http:' or 'ftp:', then we'll assume all lines
    # are absolute and just return the open file.
    check = f.read(5)
    f.seek(0, 0)
    if check == 'http:' or check[:4] == 'ftp:':
      return f

    # they're relative file names. fix them up.
    output = [ ]
    for line in f.readlines():
      line = string.strip(line)
      if line[:5] == 'http:' or line[:4] == 'ftp:':
        output.append(line)
        continue
      line = os.path.normpath(line)
      if os.path.isabs(line):
        self.log_message('bad line in "%s": %s', self.path, line)
        continue
      if not os.path.exists(os.path.join(dirpath, line)):
        self.log_message('file not found (in "%s"): %s', self.path, line)
        continue
      line = string.replace(line, "\\", "/")  # if we're on Windows
      output.append(self.build_url(url, line))

    f = StringIO.StringIO(string.join(output, '\n') + '\n')
    return f

  def serve_file(self, name, fullpath, url, range=None):
    base, ext = os.path.splitext(name)
    ext = string.lower(ext)
    if any_extensions.has_key(ext):
      if not picture_extensions.has_key(ext):
        # log the request of this file
        ip, port = self.client_address
        self.server.log_user(ip, time.time(), url + '/' + urllib.quote(name))

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
          f = self.open_playlist(fullpath, url)
          clen = len(f.getvalue())

    self.send_response(200)
    self.send_header("Content-Type", type)
    self.send_header("Content-Length", clen)
    # Thanks to Stefan Alfredsson <stefan@alfredsson.org>
    # for the suggestion, Now the filenames get displayed right.
    self.send_header("icy-name", base)
    self.end_headers()

    #Seek if the client requests it (a HTTP/1.1 request)
    if range:
      type, seek = string.split(range,'=')
      startSeek, endSeek = string.split(seek,'-')
      f.seek(int(startSeek))

    while 1:
      data = f.read(8192)
      if not data:
        break
      try:
        self.wfile.write(data)
      except ClientAbortedException:
        self.log_message('client closed connection for "%s"', self.path)
        break
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
        Messages().debug_message('DEBUG --- Warning in translate_path --- Illegal path: the \'..\' attempted to go above the root')
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
      s_buf = str(buf)
      return self.wfile.write(s_buf)
    except IOError, v:
      if v.errno == 32 or v.errno == 104:
        raise ClientAbortedException
      else:
        # not a 'Broken pipe' or Connection reset by peer
        # re-raise the error
        raise

class ClientAbortedException(Exception):
  pass

class _datablob:
  def __init__(self, **args):
    self.__dict__.update(args)

class empty_delegator:
  "Delegate attrs to another object; fill in empty string for unknown attrs."
  def __init__(self, ob):
    self.ob = ob
  def __getattr__(self, name):
    try:
      return getattr(self.ob, name)
    except AttributeError:
      return ''

class OggInfo:
  """Extra information about an Ogg Vorbis file.
  Uses ogg-python and vorbis-python from http://www.duke.edu/~ahc4/pyogg/.

  Patch from Justin Erenkrantz <justin@erenkrantz.com>
  """

  def __init__(self, name):
    self.valid = 0
    #
    # Generic File Info
    #
    self.vf = ogg.vorbis.VorbisFile(name)
    vc = self.vf.comment()
    vi = self.vf.info()

    self.bitrate = vi.rate
    # According to the docs, -1 means the current bitstream
    self.length = self.vf.time_total(-1)

    # recognized_comments = ('Artist', 'Album', 'Title', 'Version',
    #                        'Organization', 'Genre', 'Description',
    #                        'Date', 'Location', 'Copyright', 'Vendor')
    for key, val in vc.items():
        if key == 'TITLE':
            self.title = val
        elif key == 'ARTIST':
            self.artist = val
        elif key == 'ALBUM':
            self.album = val
        elif key == 'DATE':
            self.year = val
        elif key == 'GENRE':
            self.genre = val
        elif key == 'VENDOR':
            self.vendor = val
        elif key == 'TRACKNUMBER':
            self.track = val
        elif key == 'COMMENT':
            self.comment = val
        elif key == 'TRANSCODED':
            self.transcoded = val

    self.valid = 1


def _usable_file(fname):
  return fname[0] != '.'

def sort_dir(d):
  l = filter(_usable_file, os.listdir(d))
  l.sort()
  return l

def dot2int(dotaddr):
  a, b, c, d = map(int, string.split(dotaddr, '.'))
  return (a << 24) + (b << 16) + (c << 8) + (d << 0)

class Messages:
  def debug_message(self, message):
    if debug_level == 1:
      print message

# return empty string or a "new since..." string
def check_new(ctime):
  if (time.time() - ctime) < DAYS_NEW * 86400:
    t = time.strftime('%B %d', time.localtime(ctime))
    return ' <span class="isnew">new since %s</span>' % t
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
  '.ogg' : 'application/x-ogg',
  }

# Extensions of images: (and their MIME type)
picture_extensions = {
  '.gif' : 'image/gif',
  '.jpeg' : 'image/jpeg',
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
    if os.path.isfile(fname) != 1:
      print "edna: %s: No such file" % fname
      raise SystemExit
  else:
    fname = 'edna.conf'

  svr = Server(fname)
  if oggSupport == 'yes':
    print 'Ogg Vorbis support enabled'
  else:
    print 'Ogg Vorbis support disabled, to enable it you will need to install the "pyogg" and the "pyvorbis" modules'

  print "edna: serving on port %d..." % svr.port
  try:
    svr.serve_forever()
  except KeyboardInterrupt:
    print "\nCaught ctr-c, taking down the server"
    print "Please wait while the remaining streams finnish.."
    raise SystemExit

##########################################################################
#
# TODO
#
# add a server admin address to the pages
#
# server-side playlist construction
# persistent playlists (Todd Rowe <trowe@soleras.com>)
# per-user, persistent playlists (Pétur Rúnar Guðnason <prg@margmidlun.is>)
#   e.g. log in and pick up your playlists
#
# pass an MP3 (or playlist) off to a server-side player. allows remote
#   control of an MP3 jukebox/player combo.
#
# add MP3 info into the style-xml.ezt template
#          for key, val in vars(text).items():
#            if key != "valid" and val != None:
#              self.wfile.write('<%s>%s</%s>'
#                               % (cgi.escape(key), cgi.escape('%s' % (val)),
#                                  cgi.escape(key)))
#
# community building (Pétur Rúnar Guðnason <prg@margmidlun.is>)
#    - most popular songs / directories
#    - comments on songs / directories
#
# provide a mechanism for serving misc. files (e.g CSS files)
#
#
#
