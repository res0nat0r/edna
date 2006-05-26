#!/usr/bin/env python
# 
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307
# USA

import time
from threading import Thread
import time
import signal
import sys

class Scheduler(Thread):
  """
  A class whose instances are meant to be run in their own thread, which
  repetetively schedules calls to a single action function.  It differs
  from the sched module in that calls to the function are repeated every
  interval instead of being called once, and that the scheduler can be
  stopped even when the underlying platform's time.sleep() call is not
  interruptable.
  Users of this class will have to do the right thing as far as setting
  up signal handlers and catching interrupts.
  """

  def __init__(self, offset, interval, action, action_args, sleep_quantum):
    """
    Create and initialize a new scheduler object.  The caller will need to 
    call Thread.start() on it to create a thread to start scheduling actions.
    The scheduler calls action(action_args) once right when it's started,
    then every 'interval' seconds starting 'offset' seconds after midnight.
    For example scheduler(3 * 3600, 24 * 3600, foo, ['bar'], 10) will cause
    foo to be called once a day at 3am.
    The 'sleep_quantum' argument determines how long the thread will sleep
    between checks for whether the thread has been requested to stop.  This
    is needed because, on some platforms, calls to time.sleep() are not
    interrupted.
    """
    Thread.__init__(self)
    self.offset = int(offset) % (24 * 3600)
    self.interval = int(interval)
    self.action = action
    self.action_args = action_args
    self.sleep_quantum = sleep_quantum

  def run(self):
    """
    Start scheduling actions.  The action will be run first once right away,
    then repetetively thereafter accoding to self.offset and self.interval.
    """
    self.stop_requested = 0
    while not self.stop_requested:
      void = apply(self.action, self.action_args)
      next = self.next_time()
      while time.time() < next and not self.stop_requested:
	try:
	  time.sleep(min(self.sleep_quantum, next - time.time()))
	except:
	  pass

  def stop(self):
    """
    Request that this scheduler thread be stopped. Depending on the underlying
    thread implementation, it may take up to self.sleep_quantum seconds for the
    scheduler thread to find out it's supposed to stop.  Also, if the scheduler
    thread is in the middle of running the action, it won't stop until the action
    is done.
    """
    self.stop_requested = 1;
            
  def next_time(self):
    """
    Return the next time at which the action will be run.
    """
    now = int(time.time())  # we want integer arithmetic
    (x, x, x, hour, minute, second, x, x, x) = time.localtime(now)
    midnight = now - hour * 3600 - minute * 60 - second
    base = midnight + self.offset
    if base > now:
      base = base - 24 * 3600
    return base + self.interval * ((now - base) / self.interval + 1)

if __name__ == '__main__':

  # Testing

  class worker:
    def __init__(self, name):
      self.name = name
    def search(self):
      print "called search() at", int(time.time()), "name=", self.name


  got_sigint = 0
  def handle_sigint(sig, frame):
    global got_sigint
    got_sigint = 1
  signal.signal(signal.SIGINT, handle_sigint)

  def test(duration, offset, interval, sleep_quantum, workername):
    print "test: duration=",duration,"offset=",offset,"interval=",interval,"sleep_quantum=",sleep_quantum,"workername=",workername
    w = worker(workername)
    s = Scheduler(offset, interval, worker.search, [w], sleep_quantum)
    s.start()
    print "main thread sleeping"
    for i in range(1, duration):
      try:
	time.sleep(1)
      except:
	pass
      if got_sigint:
	  print "main thread got sigint, exiting"
	  s.stop()
	  sys.exit(0)
    print "main thread waking"
    s.stop()

  test(duration=10, offset=0, interval=1, sleep_quantum=10, workername="foo") # sleep_quantum > interval
  test(duration=30, offset=0, interval=5, sleep_quantum=1, workername="bar")

