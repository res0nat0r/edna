#!/usr/bin/env python
#
# edna.py -- an MP3 server
#
# Copyright (C) 1999 Greg Stein. All Rights Reserved.
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

error = __name__ + '.error'

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
  def server_bind(self):
    SocketServer.TCPServer.server_bind(self)

  def config(self, config):
    self.config = config

    dirs = [ ]
    for option in config.options('sources'):
      if option[:3] == 'dir':
        dirs.append((int(option[3:]), config.get('sources', option)))
    if not dirs:
      raise error, 'no sources'
    dirs.sort()
    for i in range(len(dirs)):
      dir = map(string.strip, string.split(dirs[i][1], '='))
      if not os.path.isdir(dir[0]):
        print 'WARNING: skipping:', dir[0]
        dirs[i] = None
        continue
      if len(dir) == 1:
        name = dir[0]
      else:
        name = dir[1]
      dirs[i] = (dir[0], name)
    self.dirs = filter(None, dirs)


class RequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):

  def do_GET(self):
    path = self.translate_path()
    if path is None:
      self.send_error(400, 'Illegal URL construction')
      return

    if not path:
      subdirs = map(lambda x: (x[1], x[1]+'/'), self.server.dirs)
      self.display_page('select music', subdirs)
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
        if p == 'all.m3u' or p == 'allrecursive.m3u':
          self.serve_file(p, curdir, url)
          return
        pathname = os.path.join(curdir, p)
        base, ext = os.path.splitext(p)
        if string.lower(ext) == '.m3u':
          base, ext = os.path.splitext(base)
          if string.lower(ext) == '.mp3':
            pathname = os.path.join(curdir, base + ext)
        if not os.path.exists(pathname):
          self.send_error(404)
          return
        if os.path.isfile(pathname):
          self.serve_file(p, pathname, url)
          return
        curdir = pathname
        url = url + '/' + urllib.quote(p)

      subdirs = []
      songs = []
      playlists = []
      tail = path[-1]
      taillen = len(path[-1])
      for name in sort_dir(curdir):
        base, ext = os.path.splitext(name)
        ext = string.lower(ext)
        if ext == '.mp3':
          # if a song has a prefix that matches the directory, then strip it
          if base[:taillen] == tail:
            base = base[taillen:]

            # trim a bit of stuff off of the file
            match = re_trim.match(base)
            if match:
              base = match.group(1)
          songs.append((base, name + '.m3u'))
        elif ext == '.m3u':
          playlists.append((base, name))
        else:
          newdir = os.path.join(curdir, name)
          if os.path.isdir(newdir):
            subdirs.append(name, name + '/')
      self.display_page('select music', subdirs, songs, playlists)

  def display_page(self, title, subdirs, songs=[], playlists=[]):
    self.send_response(200)
    self.send_header("Content-Type", "text/html")
    self.end_headers()

    self.wfile.write('<html><head><title>%s</title></head>'
                     '<body><h1>%s</h1>\n'
                     % (cgi.escape(title), cgi.escape(title))
                     )
    self.display_list('Subdirectories', subdirs)
    self.display_list('Songs', songs)
    self.display_list('Playlists', playlists)

    if not subdirs and not songs and not playlists:
      self.wfile.write('<i>Empty directory</i>\n')

    self.wfile.write('</body></html>\n')

  def display_list(self, title, list):
    if list:
      self.wfile.write('<p>%s:</p><ul>\n' % title)
      for text, href in list:
        self.wfile.write('<li><a href="%s">%s</a></li>\n' % (urllib.quote(href), cgi.escape(text)))
      self.wfile.write('</ul>\n')
      if title == 'Songs':
        self.wfile.write('<p><blockquote><a href="all.m3u">Play all songs</a></blockquote></p>\n')
      elif title == 'Subdirectories' : 
        self.wfile.write('<p><blockquote><a href="allrecursive.m3u">Play all songs (recursively)</a></blockquote></p>\n')

  def make_list(self, fullpath, url, recursive, songs=None):
    # This routine takes a string for 'fullpath' and 'url', a list for
    # 'songs' and a boolean for 'recursive'. If recursive is false make_list
    # will return a list of every file ending in '.mp3' in fullpath. If
    # recursive is true make_list will return a list of every file ending
    # in '.mp3' in fullpath and in every directory beneath fullpath.
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
      if name[-4:] == '.mp3':
        # add the song's URL to the list we're building
        songs.append(self.build_url(url, name) + '\n')

      # recurse down into subdirectories looking for more MP3s.
      if recursive and os.path.isdir(fullpath + '/' + name):
        songs = self.make_list(fullpath + '/' + name,
                               url + '/' + urllib.quote(name),
                               recursive,
                               songs)

    return songs

  def serve_file(self, name, fullpath, url):
    base, ext = os.path.splitext(name)
    ext = string.lower(ext)
    if ext == '.mp3':
      type = 'video/mpeg'
      f = open(fullpath, 'rb')
    elif ext != '.m3u':
      self.send_error(404)
      return
    else:
      type = 'audio/x-mpegurl'
      if name == 'all.m3u' or name == 'allrecursive.m3u':
        recursive = name == 'allrecursive.m3u'

        # generate the list of URLs to the songs
        songs = self.make_list(fullpath, url, recursive)

        f = StringIO.StringIO(string.join(songs, ''))
      else:
        base, ext = os.path.splitext(base)
        if ext == '.mp3':
          f = StringIO.StringIO(self.build_url(url, base) + '.mp3\n')
        else:
          f = open(fullpath)

    self.send_response(200)
    self.send_header("Content-Type", type)
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

  def build_url(self, url, file):
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


def sort_dir(d):
  l = os.listdir(d)
  l.sort()
  return l

if __name__ == '__main__':
  if len(sys.argv) > 2:
    print 'USAGE: %s [config-file]' % os.path.basename(sys.argv[0])
    print '  if config-file is not specified, then edna.conf is used'
    sys.exit(1)

  if len(sys.argv) == 2:
    fname = sys.argv[1]
  else:
    fname = 'edna.conf'

  config = ConfigParser.ConfigParser()

  # set up some defaults
  config.add_section('server')
  config.add_section('sources')
  d = config.defaults()
  d['port'] = '8080'
  d['log'] = ''

  # read the config file now
  config.read(fname)

  port = config.getint('server', 'port')

  svr = Server(('', port), RequestHandler)
  svr.config(config)

  print "edna: serving on port %d..." % port
  svr.serve_forever()
