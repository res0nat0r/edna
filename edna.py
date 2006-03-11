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
#   $Id: edna.py,v 1.83 2006/03/11 19:36:29 syrk Exp $
#

__version__ = '0.6'

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
import zipfile
import ezt
import MP3Info
import md5

try:
  import signal
  signalSupport = 'yes'
except ImportError:
  signalSupport = 'no'

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

try:
  import sha
except ImportError:
  pass

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
    d['robots'] = '1'
    d['binding-hostname'] = ''
    d['name_prefix'] = ''
    d['log'] = ''
    d['template-dir'] = 'templates'
    d['template'] = 'default.ezt'
    d['resource-dir'] = 'resources';
    d['auth_level'] = '1'
    d['debug_level'] = '0'
    d['fileinfo'] = '0'
    d['encoding'] = 'UTF-8,iso8859-1'
    d['hide_names'] = ""
    d['hide_matching'] = ""
    d['zip'] = '0'

    config.read(fname)

    # Setup a logging file
    self.log = None
    log = self.config.get('server', 'log')
    if log:
      if log == '-':
        self.log = sys.stdout
      else:
        try:
          self.log = open(log, 'a')
        except IOError:
          pass
    
    template_path = config.get('server', 'template-dir')
    template_file = config.get('server', 'template')
    template_path = os.path.join(os.path.dirname(fname), template_path)
    self.resource_dir = os.path.join(os.path.dirname(fname), config.get('server', 'resource-dir'))
    self.fileinfo = config.getint('server', 'fileinfo')
    self.zipmax = config.getint('server', 'zip') * 1024 * 1024
    self.zipsize = 0

    global debug_level
    debug_level = config.getint('extra', 'debug_level')
    global DAYS_NEW
    DAYS_NEW = config.getint('extra', 'days_new')
    global HIDE_EXACT
    HIDE_EXACT = filter(None, [toHide.strip().lower() for toHide in config.get('extra', 'hide_names').split(',')])
    global HIDE_MATCH
    HIDE_MATCH = filter(None, [toHide.strip().lower() for toHide in config.get('extra', 'hide_matching').split(',')])

    if debug_level == 1:
      self.log_message('Running in debug mode')

    encodings = string.split(config.get('server', 'encoding'), ',')
    tfname = os.path.join(template_path, template_file)
    self.default_template = ezt.Template(tfname, encodings)

    tfname = os.path.join(template_path, 'style-xml.ezt')
    self.xml_template = ezt.Template(tfname, encodings)

    tfname = os.path.join(template_path, 'stats.ezt')
    self.stats_template = ezt.Template(tfname, encodings)

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
        self.log_message("WARNING: a source's directory must exist")
        self.log_message(" skipping: dir%d = %s = %s" % (dirs[i][0], dir[0], name))
        continue
      if string.find(name, '/') != -1:
        self.log_message("WARNING: a source's display name cannot contain '/'")
        self.log_message(" skipping: dir%d = %s = %s" % (dirs[i][0], dir[0], name))
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
      
      try:
        self.password_hash = config.get('acl','password_hash')
        
        if not globals().has_key(self.password_hash):
          self.log_message("WARNING: there is no hash module '%s' for passwords" % \
                                                                  self.password_hash)
          self.password_hash = None
        else:
          self.debug_message("passwords authenticated using %s hexdigest" % \
                                                                  self.password_hash)
          
      except ConfigParser.NoOptionError:
        self.password_hash = None
        
      if self.password_hash is None:
        self.debug_message("passwords authenticated in plain text")
        
      for pair in auth_pairs:
        user,passw = string.split(pair,':')
        self.auth_table[user] = passw
    except ConfigParser.NoOptionError:
      self.auth_table = {}

    self.auth_level = config.get('acl', 'auth_level')

    self.name_prefix = config.get('server', 'name_prefix')

    self.port = config.getint('server', 'port')
    try:
        SocketServer.TCPServer.__init__(self,
            (config.get('server', 'binding-hostname'), self.port),
            EdnaRequestHandler)
    except socket.error, value:
        self.log_message( "edna: bind(): %s" % str(value[1]) )
        raise SystemExit

  def server_bind(self):
    # set SO_REUSEADDR (if available on this platform)
    if hasattr(socket, 'SOL_SOCKET') and hasattr(socket, 'SO_REUSEADDR'):
      self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    BaseHTTPServer.HTTPServer.server_bind(self)

  def log_user(self, ip, tm, url):
    if len(self.userLog) > 40:
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

  def log_message(self, msg):
    if self.log:
      try:
        self.log.write(msg + '\n')
        self.log.flush()
      except IOError:
        pass

  def debug_message(self, msg):
    if debug_level<1:
      return
    self.log_message ('DEBUG: ' + msg)

class EdnaRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):

  def do_GET(self):
    try:
      self._perform_GET()
    except ClientAbortedException:
      self.server.debug_message('Exception caught in "do_GET" --- ClientAbortException')
    except IOError:
      pass

  def check_authorization(self):
    auth_table = self.server.auth_table
    auth = self.headers.getheader('Authorization')
    this_user, this_pass = None, None
    if auth:
      def transl(passwd):
        hash = globals()[self.server.password_hash]
        return hash.new(passwd).hexdigest()
        
      if not self.server.password_hash:
        transl = str # i.e. no translation
        
      if string.lower(auth[:6]) == 'basic ':
        import base64
        [name,password] = string.split(
          base64.decodestring(string.split(auth)[-1]), ':')
        this_user, this_pass = name, password
      
      this_pass = transl(this_pass)
      if auth_table.has_key(this_user) and auth_table[this_user] == this_pass:
        self.server.debug_message('--- Authenticated --- User: %s Password: %s' % \
                                                        (this_user, this_pass))
        return 1

      self.server.debug_message('--- Auth FAILED --- User: %s Password: %s' % \
                                (this_user, this_pass))
      if not auth_table.has_key(this_user):
        self.server.debug_message('--- User does not exist --- %s' % this_user)

    realm='edna'
    self.send_response(401)
    self.send_header('WWW-Authenticate', 'Basic realm="%s"' % realm)
    self.send_header('Content-Type', 'text/html;');
    self.send_header('Connection', 'close');
    self.end_headers()
    try:
      short, long = self.responses[401]
    except KeyError:
      short, long = '???', '???'

    self.wfile.write(self.error_message_format %
                     {'code': 401, 'message': short, 'explain': long})
      
    return 0
    
  def _perform_GET(self):

    ## verify the IP
    if not self.server.acl_ok(self.client_address[0]):
      self.send_error(403, 'Forbidden')
      return

    ## verify the Username/Password
    if self.server.auth_table:
      if self.server.auth_level == '2' or \
         ( self.server.auth_level == '1' and self.path[-1] == '/' ) or \
         self.path == '/':
        if not self.check_authorization():
          return

    path = self.translate_path()
    if path is None:
      self.send_error(400, 'Illegal URL construction')
      return

    if path == ["robots.txt"] and self.server.config.getint('server', 'robots') != 0:
      self.send_response(200)
      self.send_header("Content-Type", "text/plain")
      self.end_headers()
      self.wfile.write("User-agent: *\nDisallow /\n")
      return

    self.output_style = 'html'
    if len(path) >= 1:
      if path[0] == 'xml':
          path.pop(0)
          self.output_style = 'xml'

    if not path and len(self.server.dirs) > 1:
      # home page
      subdirs = [ ]
      for d, name in self.server.dirs:
        subdirs.append(_datablob(href=urllib.quote(name) + '/', is_new='',
                                 text=name))
      self.display_page(TITLE, subdirs, skiprec=1)
    elif path and path[0] == 'stats':
      # the site statistics were requested
      self.display_stats()
    elif path and path[0] == 'resources' and len(path) > 1:
      # a resource file was requested
      fullpath = os.path.join(self.server.resource_dir, path[1])
      self.serve_file(path[1], fullpath, '/resources');
    else:
      # other requests fall under the user configured namespace
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
           p == 'shuffle.m3u' or p == 'shufflerecursive.m3u' or \
           p == 'all.zip':
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
      plainfiles = []

      if path:
        thisdir = path[-1]
      else:
        # one of the top-level virtual directories
        thisdir = ''
      thisdirlen = len(thisdir)

      for name in sort_dir(curdir):
        href = urllib.quote(name)
        try:
          is_new = check_new(os.stat(os.path.join(curdir, name))[stat.ST_MTIME])
        except: 
          # For example, in the case of disk I/O errors
          print "Failed to stat %s"%(name)
          continue
        nameLower = name.lower()
        if nameLower in HIDE_EXACT: continue
        skip = False
        for toHide in HIDE_MATCH:
          if toHide in nameLower: 
            self.server.debug_message("Hiding %s"%(name))
            # I can't find a way to "continue" up two levels with one call...
            skip = True
            continue
          if skip:
            continue

        base, ext = os.path.splitext(name)
        ext = string.lower(ext)

        if picture_extensions.has_key(ext):
          pictures.append(_datablob(href=href, is_new=is_new))
          continue

        if plainfiles_extensions.has_key(ext):
          plainfiles.append(_datablob(href=href, is_new=is_new, text=base))
          continue

        if ext == '.m3u':
          playlists.append(_datablob(href=href, is_new=is_new, text=base))
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

          d = _datablob(href=href, is_new=is_new, text=base)
          if self.server.fileinfo:
            info = FileInfo(fullpath)
          else:
            info = _datablob()
          d.info = empty_delegator(info)

          songs.append(d)
        else:
          newdir = os.path.join(curdir, name)
          if os.path.isdir(fullpath):
            subdirs.append(_datablob(href=href + '/', is_new=is_new, text=name))

      self.display_page(title, subdirs, pictures, plainfiles, songs, playlists)

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

  def display_page(self, title, subdirs, pictures=[], plainfiles=[], songs=[], playlists=[],
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
             'plainfiles' : plainfiles,
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
      if url:
        base, ext = os.path.splitext(name)
        if extensions.has_key(string.lower(ext)):
          # add the song's URL to the list we're building
          songs.append(self.build_url(url, name) + '\n')
      else:
        if os.path.isfile(fullpath + '/' + name):
          songs.append(name)

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

    output = [ ]
    for line in f.readlines():
      line = string.strip(line)
      if line[:7] == '#EXTM3U' or line[:8] == '#EXTINF:':
        output.append(line)
        continue
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
    mtime = None
    if any_extensions.has_key(ext):
      if not picture_extensions.has_key(ext):
        # log the request of this file
        ip, port = self.client_address
        self.server.log_user(ip, time.time(), url + '/' + urllib.quote(name))

      # get the file and info for delivery
      type = any_extensions[ext]
      f = open(fullpath, 'rb')
      st = os.fstat(f.fileno())
      clen = st.st_size
      mtime = st.st_mtime
    elif url == '/resources':
      # We don't want to serve pseudo files under /resources
      self.send_error(404)
      return
    elif ext == '.m3u':
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
          mtime = os.stat(fullpath)[stat.ST_MTIME]
    elif name == 'all.zip':
      if not self.server.zipmax > 0:
        self.send_error(403, 'The ZIP service has been disabled by the server administrator.')
        return

      type = 'application/zip'
      f = StringIO.StringIO()
      z = zipfile.ZipFile(f, 'w', zipfile.ZIP_STORED)
      songs = self.make_list(fullpath, None, None, None)
      for s in songs:
        z.write(fullpath + '/' + s, os.path.basename(fullpath) + '/' + s)
        if self.server.zipsize + len(f.getvalue()) > self.server.zipmax:
          break

      z.close()
      f.seek(0)
      clen = len(f.getvalue())
      self.server.debug_message("ZUP thresholds: %d + %d vs %d" %
                                (self.server.zipsize, clen, self.server.zipmax))

      if self.server.zipsize + clen > self.server.zipmax:
        self.send_error(503, 'The <b>ZIP</b> service is currently under heavy load.  Please try again later.')
        return

      self.server.zipsize += clen
    else:
      self.send_error(404)
      return

    self.send_response(200)
    self.send_header("Content-Type", type)
    self.send_header("Content-Length", clen)
    if mtime:
      self.send_header('Last-Modified', time.strftime("%a, %d %b %Y %T GMT"))
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

    if type == 'application/zip':
      self.server.zipsize -= clen

  def build_url(self, url, file=''):
    host = self.server.name_prefix or self.headers.getheader('host') or self.server.server_name
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
        self.server.debug_message('Warning in translate_path --- Illegal path: the \'..\' attempted to go above the root')
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
    if not self.server.log:
      return
    msg = "%s [%s] %s" % (self.address_string(),
                          self.log_date_time_string(),
                          format % args)
    self.server.log_message (msg)

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
    if hasattr(self.ob, name):
      return getattr(self.ob, name)
    else:
      return ''


class FileInfo:
  """Grab as much info as you can from the file given"""

  def __init__(self, fullpath):
    base, ext = os.path.splitext(fullpath)
    ext = string.lower(ext)

    if ext == '.ogg':
      info = OggInfo(fullpath)
      self.__dict__.update(info.__dict__)
    else:
      info = MP3Info.MP3Info(open(fullpath, 'rb'))
      self.__dict__.update(info.__dict__)
      self.total_time = info.mpeg.total_time;
      self.filesize = info.mpeg.filesize2
      self.bitrate = int(info.mpeg.bitrate)
      self.samplerate = info.mpeg.samplerate/1000
      self.mode = info.mpeg.mode
      self.mode_extension = info.mpeg.mode_extension

      
#    if hasattr(info, 'length'):
    if self.total_time > 3600:
      self.duration = '%d:%02d:%02d' % (int(self.total_time / 3600),
                                          int(self.total_time / 60) % 60,
                                          int(self.total_time) % 60)
    elif self.total_time > 60:
      self.duration = '%d:%02d' % (int(self.total_time / 60),
                                     int(self.total_time) % 60)
    else:
      self.duration ='%02d' % int(self.total_time)
    
    
class OggInfo:
  """Extra information about an Ogg Vorbis file.
  Uses ogg-python and vorbis-python from http://www.duke.edu/~ahc4/pyogg/.

  Patch from Justin Erenkrantz <justin@erenkrantz.com>
  """

  def __init__(self, name):
    global oggSupport

    # Setup the defaults
    self.valid = 0
    self.total_time = 0
    self.samplerate = 'unkown'
    self.bitrate = 'unkown'
    self.mode = ''
    self.mode_extension = ''

    if oggSupport == 'no':
      return

    #
    # Generic File Info
    #
    vf = ogg.vorbis.VorbisFile(name)
    vc = vf.comment()
    vi = vf.info()

    # According to the docs, -1 means the current bitstream
    self.samplerate = vi.rate
    self.total_time = vf.time_total(-1)
    self.bitrate = vf.bitrate(-1) / 1000 
    self.filesize = vf.raw_total(-1)/1024/1024

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
  '.avi' : 'video/x-msvideo',
  '.mpg' : 'video/mpeg',
  '.ogg' : 'application/x-ogg',
  '.m4a' : 'audio/mp4',
  '.mp4' : 'video/mp4',  
  }

# Extensions of images: (and their MIME type)
picture_extensions = {
  '.gif' : 'image/gif',
  '.jpeg' : 'image/jpeg',
  '.jpg' : 'image/jpeg',
  '.png' : 'image/png',
  }

# Extensions of non-streamed, non-media files we want to serve: (and their MIME type)
plainfiles_extensions = {
  '.txt' : 'text/plain',
  }

any_extensions = {}
any_extensions.update(extensions)
any_extensions.update(picture_extensions)
any_extensions.update(plainfiles_extensions)

config_needed = None
running = 1

def sighup_handler(signum, frame):
  global config_needed
  config_needed = 1

def sigterm_handler(signum, frame):
  global running
  running = None

def run_server(fname):
  global running, config_needed, oggSupport

  if signalSupport == 'yes':
    if 'SIGHUP' in dir(signal):
      signal.signal(signal.SIGHUP, sighup_handler)
    if 'SIGTERM' in dir(signal):
      signal.signal(signal.SIGTERM, sigterm_handler)

  svr = Server(fname)
  if oggSupport == 'yes':
    svr.log_message('edna: Ogg Vorbis support enabled')
  else:
    svr.log_message('edna: Ogg Vorbis support disabled, to enable it you will need to install the "pyogg" and the "pyvorbis" modules')

  svr.log_message("edna: serving on port %d..." % svr.port)
  try:
    while running:
#      print 'waiting ... '
      if config_needed:
        svr.log_message('edna: Reloading config %s' % fname)
        svr.server_close()
        svr = Server(fname)
        config_needed  = None
      svr.handle_request()
    svr.log_message ("edna: exiting")
    sys.exit(0)
  except KeyboardInterrupt:
    print "\nCaught ctr-c, taking down the server"
    print "Please wait while the remaining streams finish.."
    sys.exit(0)

def usage():
      print 'USAGE: %s [--daemon] [config-file]' % os.path.basename(sys.argv[0])
      print '  if config-file is not specified, then edna.conf is used'
      sys.exit(0)

def daemonize(stdin='/dev/null', stdout='/dev/null', stderr='/dev/null',pname=''):
    '''This forks the current process into a daemon.
    The stdin, stdout, and stderr arguments are file names that
    will be opened and be used to replace the standard file descriptors
    in sys.stdin, sys.stdout, and sys.stderr.
    These arguments are optional and default to /dev/null.
    Note that stderr is opened unbuffered, so
    if it shares a file with stdout then interleaved output
    may not appear in the order that you expect.
    '''
    # Do first fork.
    try: 
        pid = os.fork() 
        if pid > 0:
            sys.exit(0) # Exit first parent.
    except OSError, e: 
        sys.stderr.write("fork #1 failed: (%d) %s\n" % (e.errno, e.strerror)    )
        sys.exit(1)
        
    # Decouple from parent environment.
    os.chdir("/") 
    os.umask(0) 
    os.setsid() 
    
    # Do second fork.
    try: 
        pid = os.fork() 
        if pid > 0:
            if pname:
              pidfile = open(pname, 'w')
              pidfile.write(str (pid))
              pidfile.close()
            sys.exit(0) # Exit second parent.
    except OSError, e: 
        sys.stderr.write("fork #2 failed: (%d) %s\n" % (e.errno, e.strerror)    )
        sys.exit(1)
    # Now I am a daemon!
    # Redirect standard file descriptors.
    si = open(stdin, 'r')
    so = open(stdout, 'a+')
    se = open(stderr, 'a+', 0)
    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())


if __name__ == '__main__':
  fname = 'edna.conf'
  daemon_mode=0
  for a in sys.argv[1:]:
    if a == "--daemon":
      daemon_mode=1
    elif a == "--help" or a == "-h" or string.find(a, '--')==0:
      usage()
    else:
      fname = a

  if os.path.isfile(fname) != 1:
    print "edna: %s:No such file" %fname
    raise systemExit

  if daemon_mode:
    daemonize('/dev/null', '/var/log/edna.log', '/var/log/edna.log', '/var/run/edna.pid')

  run_server(fname)
