import win32serviceutil
from win32event import *
from win32file import *
import SocketServer
import socket
import win32service
import win32api
import win32con
import traceback
import sys
import thread
import StringIO

SvcShutdown = "SvcShutdown"

class SvcTrackingThreadingMixin:
  """
      The purpose of this mixin is to track thread handles so that the service
      won't shut down until all of the pending requests are finished.
  """
  def process_request(self, request, client_address):
    """Start a new thread to process the request."""
    thread.start_new_thread(self.manage_request,
                            (request, client_address))
  def manage_request(self, request, client_address):
    try:
      procHandle = win32api.GetCurrentProcess()
      th = win32api.DuplicateHandle(procHandle, win32api.GetCurrentThread(), procHandle, 0, 0, win32con.DUPLICATE_SAME_ACCESS)
      try:
        # Pretend Python doesn't have the interpreter lock.
        self.lckThreadHandles.acquire()
        self.thread_handles.append(th)
        self.lckThreadHandles.release()
        try:
          self.finish_request(request, client_address)
        except:
          s = StringIO.StringIO()
          traceback.print_exc(file=s)
          self.servicemanager.LogErrorMsg(s.getvalue())
      finally:
        # Pretend Python doesn't have the interpreter lock...
        self.lckThreadHandles.acquire()
        self.thread_handles.remove(th)
        self.lckThreadHandles.release()
    except:
      s = StringIO.StringIO()
      traceback.print_exc(file=s)
      self.servicemanager.LogErrorMsg(s.getvalue())


class TCPServerService(win32serviceutil.ServiceFramework):
  """
  This class is the glue between acting as an NT service, and
  any arbitrary SocketServer.TCPServer.
  """
  def __init__(self, args):
    # Kick off the NT service framework.
    # This registers win32serviceutil.ServiceFramework.ServiceCtrlHandler
    # as the Python routine to be invoked when SCM (Service Control Mangager)
    # notifications arrive.
    win32serviceutil.ServiceFramework.__init__(self, args)
    # Create the necessary NT Event synchronization objects...
    # hevSvcStop is signaled when the SCM sends us a notification to shutdown
    # the service.
    self.hevSvcStop = CreateEvent(None, 0, 0, None)
    # hevConn is signaled when we have a new incomming connection.
    self.hevConn    = CreateEvent(None, 0, 0, None)
    # This is used if there is a mixin that needs to prevent
    # service shutdown if there are outstanding requests.
    self.thread_handles = []
    # This is us pretending that Python doesn't have the interperter lock.
    self.lckThreadHandles = thread.allocate_lock()
    # Hang onto this module for other people to use for logging purposes.
    import servicemanager
    self.servicemanager = servicemanager
    
  def SvcStop(self):
    self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
    SetEvent(self.hevSvcStop)
    
  def SvcDoRun(self):
    try:
      self.serve_forever()
    except SvcShutdown:
      pass
    except:
      s = StringIO.StringIO()
      traceback.print_exc(file=s)
      self.servicemanager.LogErrorMsg(s.getvalue())

    # The request processing mixin that manages this list is optional...
    if hasattr(self, "waitForPendingRequests"):
      # waitForPendingRequests() is overideable...
      # Let somebody else decide what the policy is.
      try:
        self.waitForPendingRequests()
      except:
        s = StringIO.StringIO()
        traceback.print_exc(file=s)
        self.servicemanager.LogErrorMsg(s.getvalue())


  def get_request(self):
    # Call WSAEventSelect to enable self.socket to be waited on.
    WSAEventSelect(self.socket, self.hevConn, FD_ACCEPT)
    while 1:
      try:
        rv = self.socket.accept()
      except socket.error, why:
        if why[0] != WSAEWOULDBLOCK:
          raise
        # Use WaitForMultipleObjects instead of select() because
        # on NT select() is only good for sockets, and not general NT
        # synchronization objects.
        rc = WaitForMultipleObjects((self.hevSvcStop, self.hevConn), 0, INFINITE)
        if rc == WAIT_OBJECT_0:
          # self.hevSvcStop was signaled, this means:
          # Stop the service!
          # So we throw the shutdown exception, which gets
          # caught by self.SvcDoRun
          raise SvcShutdown
        # Otherwise, rc == WAIT_OBJECT_0 + 1 which means self.hevConn
        # was signaled, which means when we call self.socket.accept(),
        # we'll have our incoming connection socket!
        # Loop back to the top, and let that accept do its thing...
      else:
        # yay! we have a connection
        # However... the new socket is non-blocking, we need to set it back
        # into blocking mode. (The socket that accept() returns has the
        # same properties as the listening sockets, this includes any
        # properties set by WSAAsyncSelect, or WSAEventSelect, and whether
        # its a blocking socket or not.)
        #
        # So if you yank the following line, the setblocking() call will be
        # useless. The socket will still be in non-blocking mode.
        WSAEventSelect(rv[0], self.hevConn, 0)
        rv[0].setblocking(1)
        break
    return rv
