#
# ezt.py -- easy templating
#
# Copyright (C) 2001 Greg Stein. All Rights Reserved.
#
# Redistribution and use in source and binary forms, with or without 
# modification, are permitted provided that the following conditions are 
# met:
#
# * Redistributions of source code must retain the above copyright 
#   notice, this list of conditions and the following disclaimer. 
#
# * Redistributions in binary form must reproduce the above copyright 
#   notice, this list of conditions and the following disclaimer in the 
#   documentation and/or other materials provided with the distribution. 
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS 
# IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, 
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR 
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE 
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR 
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF 
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS 
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN 
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) 
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE 
# POSSIBILITY OF SUCH DAMAGE.
#
#
# This software is maintained by Greg and is available at:
#    http://edna.sourceforge.net/
#

import string
import re

_re_parse = re.compile('(\[[-\w.# ]+\])|(\[\[\])')

# block commands and their argument counts
_block_cmd_specs = { 'if-any':1, 'if-index':2, 'for':1 }
_block_cmds = _block_cmd_specs.keys()

class Template:

  def __init__(self, fname, encodingslist):
    if fname:
      self.parse_file(fname)
    global encodings
    encodings = encodingslist

  def parse_file(self, fname):
    self.parse(open(fname).read())

  def parse(self, text):
    # parse the program into: (TEXT DIRECTIVE BRACKET)* TEXT
    # DIRECTIVE will be '[directive]' or None
    # BRACKET will be '[[]' or None
    parts = _re_parse.split(text)

    program = [ ]
    stack = [ ]

    for i in range(len(parts)):
      piece = parts[i]
      which = i % 3  # discriminate between: TEXT DIRECTIVE BRACKET
      if which == 0:
        # TEXT. append if non-empty.
        if piece:
          program.append(piece)
      elif which == 2:
        # BRACKET directive. append '[' if present.
        if piece:
          program.append('[')
      elif piece:
        # DIRECTIVE is present.
        args = piece[1:-1].split()
        cmd = args[0]
        if cmd == '#':
          # comment
          continue
        if cmd == 'else':
          if len(args) > 1:
            raise ArgCountSyntaxError()
          ### check: don't allow for 'for' cmd
          idx = stack[-1][1]
          true_section = program[idx:]
          del program[idx:]
          stack[-1][3] = true_section
        elif cmd == 'end':
          if len(args) > 1:
            raise ArgCountSyntaxError()
          # note: true-section may be None
          cmd, idx, args, true_section = stack.pop()
          else_section = program[idx:]
          func = getattr(self, '_cmd_' + re.sub('-', '_', cmd))
          program[idx:] = [ (func, (args, true_section, else_section)) ]
        elif cmd in _block_cmds:
          if len(args) > _block_cmd_specs[cmd] + 1:
            raise ArgCountSyntaxError()
          ### this assumes arg1 is always a ref
          args[1] = _prepare_ref(args[1])
          # remember the cmd, current pos, args, and a section placeholder
          stack.append([cmd, len(program), args[1:], None])
        else:
          # implied PRINT command
          if len(args) > 1:
            raise ArgCountSyntaxError()
          program.append((self._cmd_print, _prepare_ref(args[0])))

    self.program = program

  def generate(self, fp, data):
    ctx = _context()
    ctx.data = data
    ctx.for_index = { }
    self._execute(self.program, fp, ctx)

  def _execute(self, program, fp, ctx):
    for step in program:
      if isinstance(step, str):
        fp.write(step)
      else:
        step[0](step[1], fp, ctx)

  def _cmd_print(self, refs, fp, ctx):
    ### type check the value
    (refname, ref) = refs
    fp.write(_get_value(refname, ref, ctx))

  def _cmd_if_any(self, args, fp, ctx):
    "If the value is a non-empty string or non-empty list, then T else F."
    (((refname, ref),), t_section, f_section) = args
    value = _get_value(refname, ref, ctx)
    self._do_if(value, t_section, f_section, fp, ctx)

  def _cmd_if_index(self, args, fp, ctx):
    (((refname, ref), value), t_section, f_section) = args
    list, idx = ctx.for_index[refname]
    if value == 'even':
      value = idx % 2 == 0
    elif value == 'odd':
      value = idx % 2 == 1
    elif value == 'last':
      value = idx == len(list)-1
    else:
      value = idx == int(value)
    self._do_if(value, t_section, f_section, fp, ctx)

  def _do_if(self, value, t_section, f_section, fp, ctx):
    if t_section is None:
      t_section = f_section
      f_section = None
    if value:
      section = t_section
    else:
      section = f_section
    if section is not None:
      self._execute(section, fp, ctx)

  def _cmd_for(self, args, fp, ctx):
    (((refname, ref),), unused, section) = args
    list = _get_value(refname, ref, ctx)
    if isinstance(list, str):
      raise NeedSequenceError()
    ctx.for_index[refname] = [ list, 0 ]
    for i in range(len(list)):
      ctx.for_index[refname][1] = i
      self._execute(section, fp, ctx)
    del ctx.for_index[refname]


def _prepare_ref(refname):
  return refname, refname.split('.')

def _get_value(refname, ref, ctx):
  if ref[0] in ctx.for_index:
    list, idx = ctx.for_index[ref[0]]
    ob = list[idx]
  elif ref[0] in ctx.data:
    ob = ctx.data[ref[0]]
  else:
    raise UnknownReference(refname)

  # walk the dotted ref
  for attr in ref[1:]:
    try:
      ob = getattr(ob, attr)
    except AttributeError:
      raise UnknownReference(refname)

  if ob is None:
    return ''

  # make sure we return a UTF-8 string.  ### other types?
  if isinstance(ob, int) or isinstance(ob, float):
    return unicode(ob).encode('UTF-8')
  if isinstance(ob, str):
    return ob.encode('UTF-8')

  return ob

class _context:
  pass

class ArgCountSyntaxError(Exception):
  pass

class UnknownReference(Exception):
  pass

class NeedSequenceError(Exception):
  pass
