"""
This is an example of wrapping an arbitary SocketServer.TCPServer derived
server into an NT service with out too much pain and suffering.
"""

# Edna is the arbitrary TCPServer
import edna
# TCPServerService is the generic NT servic <-> TCPServer
# interaction code
import TCPServerService
# Mark's cool helper module...
import win32serviceutil
import win32service
import win32event
import SocketServer
import BaseHTTPServer

class EdnaSvc(TCPServerService.TCPServerService,
              # This following line is optional...
              # You can let edna pick up the normal threading mixin if
              # you want your service to end immediately, and abort
              # any pending requests, instead of making the service
              # hang around until all pending requests are finished.
              TCPServerService.SvcTrackingThreadingMixin,
              BaseHTTPServer.HTTPServer):
  # Here belong some special attributes pertaining to the installation
  # of this service.
  # _svc_name_, and _svc_display_name_ are required!

  # _svc_name_ is what you use when you want to start/stop the service.
  # e.g.: "net start edna", "net stop edna"
  # This is also the name of the registry key, where your configuration
  # settings are stored.
  # i.e. HKLM\SYSTEM\CurrentControlSet\Services\Edna
  _svc_name_ = "Edna"
  # _svc_display_name_ is the nice name the control panel will display
  # for your service
  _svc_display_name_ = "Edna MP3 Streaming Service"
  # The following are optional
  # _svc_deps_: This should be set to the list of service names
  #             That need to be started before this one.
  # _exe_name_: This should be set to a service .EXE if you're not
  #             going to use PythonService.exe
  
  def __init__(self, args):
    fname = win32serviceutil.GetServiceCustomOption(self, "ConfigurationFile")
    prh = edna.PseudoRequestHandler(fname)
    self.port = prh.config.getint('server', 'port')
    TCPServerService.TCPServerService.__init__(self, args, (prh.config.get('server', 'binding-hostname'), self.port), prh)

  def waitForPendingRequests(self):
    """
    Wait for any pending requests to finish/close...

    This only does anything useful if a mixin similar to
    TCPServerService.SvcTrackingThreadingMixin is in the class definition.
    """
    while self.thread_handles:
      # The 5000 says, SCM, I think I'm going to finish shutting down in 5s.
      self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, 5000)
      #print "Waiting for %d threads to finish..." % (len(self.thread_handles))
      # Actually wait for 3s just to be paranoid.
      rc = win32event.WaitForMultipleObjects(self.thread_handles, 1, 3000)
      # Keep looping until ALL threads go away...
      # You might want to have a maximum time out here to force a shutdown
      # if for example it we've gone 5+ min, and we still have some threads
      # that still haven't shutdown.
      
  def server_bind(self):
    # set SO_REUSEADDR (if available on this platform)
    if hasattr(socket, 'SOL_SOCKET') and hasattr(socket, 'SO_REUSEADDR'):
      self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    BaseHTTPServer.HTTPServer.server_bind(self)
    
    
ConfigFileNotFound = "ConfigFileNotFound"

def customOptionHandler(opts):
    "This is only called when the service is installed."
    fFoundConfigFile = 0
    for opt, val in opts:
      if opt == "-c":
        # This installs the location of the Edna configuration file into:
        # HKLM\SYSTEM\CurrentControlSet\Services\Edna\Parameters\ConfigurationFile
        win32serviceutil.SetServiceCustomOption(
          EdnaSvc._svc_name_,
          "ConfigurationFile",
          val)
        fFoundConfigFile = 1
    if not fFoundConfigFile:
      print "Error: You forgot to pass in a path to your Edna configuration file., use the '-c' option."
      raise ConfigFileNotFound
    
if __name__ == "__main__":
    import sys, regsetup
    # This magic function, handles service installation/removal, etc....
    # Sample command lines:
    # Installation:
    #   python ednaNTSvc.py -c d:\edna\edna.conf --startup auto install
    # Removal:
    #   python ednaNTSvc.py remove
    # etc...
    win32serviceutil.HandleCommandLine(
      EdnaSvc,
      argv = sys.argv,
      customInstallOptions = "c:",
      customOptionHandler = customOptionHandler)    
    # Make sure these files are in the Python path...
    regsetup.FindRegisterModule("ednaNTSvc", 'ednaNTSvc.py', sys.path)
    regsetup.FindRegisterModule("TCPServerService", 'TCPServerService.py', sys.path)
    regsetup.FindRegisterModule("edna", "edna.py", ".")
    
