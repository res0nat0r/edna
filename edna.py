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
#   $Id: edna.py,v 1.3 2000/01/27 12:26:56 gstein Exp $
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
    # we don't need the server name/port, so skip BaseHTTPServer's work
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
      self.display_page('select music', subdirs, skiprec=1)
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
          self.serve_file(p, pathname, url)
          return
        curdir = pathname
        url = url + '/' + urllib.quote(p)

      # requested a directory. ensure there is a trailing slash so that
      # the (relative) href values will work.
      if self.path[-1] != '/':
        redir = self.build_url('/' + string.join(path, '/'))
        self.redirect(redir)
        return

      subdirs = []
      songs = []
      playlists = []
      tail = path[-1]
      taillen = len(tail)
      for name in sort_dir(curdir):
        base, ext = os.path.splitext(name)
        ext = string.lower(ext)
        if extensions.has_key(ext):
          # if a song has a prefix that matches the directory, and something
          # exists after that prefix, then strip it
          if base[:taillen] == tail and len(base) > taillen:
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

  def display_page(self, title, subdirs, songs=[], playlists=[], skiprec=0):
    self.send_response(200)
    self.send_header("Content-Type", "text/html")
    self.end_headers()

    self.wfile.write('<html><head><title>%s</title></head>'
                     '<body><h1>%s</h1>\n'
                     % (cgi.escape(title), cgi.escape(title))
                     )
    self.display_list('Subdirectories', subdirs, skiprec)
    self.display_list('Songs', songs)
    self.display_list('Playlists', playlists)

    if not subdirs and not songs and not playlists:
      self.wfile.write('<i>Empty directory</i>\n')

    self.wfile.write('</body></html>\n')

  def display_list(self, title, list, skipall=0):
    if list:
      self.wfile.write('<p>%s:</p><ul>\n' % title)
      for text, href in list:
        self.wfile.write('<li><a href="%s">%s</a></li>\n' % (urllib.quote(href), cgi.escape(text)))
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
      if extensions.has_key(name[-4:]):
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

  def serve_file(self, name, fullpath, url):
    base, ext = os.path.splitext(name)
    ext = string.lower(ext)
    if extensions.has_key(ext):
      type = extensions[ext]
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
          f = open(fullpath)
          clen = os.fstat(f.fileno())[stat.ST_SIZE]

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

class ID3:
  def __init__(self, file):
    self.valid = 0
    file.seek(-128, 2)	# 128 bytes before the end of the file
    if file.read(3) == 'TAG':
      self.title = string.strip(file.read(30))
      self.artist = string.strip(file.read(30))
      self.album = string.strip(file.read(30))
      self.year = string.strip(file.read(4))
      self.comment = string.strip(file.read(30))	### 29 or 30??

      genre = ord(file.read(1))
      if genre < len(_genres):
        self.genre = _genres[genre]
      else:
        self.genre = 'unknown'

      self.valid = 1

    file.seek(0, 0)

def sort_dir(d):
  l = os.listdir(d)
  l.sort()
  return l

# Extensions that WinAMP can handle: (and their MIME type if applicable)
extensions = { 
  '.mp3' : 'video/mpeg',
  '.mid' : 'audio/mid',
  '.mp2' : 'video/mpeg',
#  '.cda',
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
  }

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
