# 
# Makefile to install edna
#

PREFIX=/usr/local
LIBDIR=$(DESTDIR)$(PREFIX)/lib/edna
BINDIR=$(DESTDIR)$(PREFIX)/bin
INITDIR=$(DESTDIR)/etc/init.d
CONFDIR=$(DESTDIR)/etc/edna

all:
	@echo "Please adjust values in edna.conf and use" 
	@echo "make install"
	@echo "or make install-daemon"


install:  
	install -d $(BINDIR) $(LIBDIR) $(CONFDIR)/templates $(LIBDIR)/resources
	install edna.py $(BINDIR)/edna
	install ezt.py $(LIBDIR)
	install MP3Info.py $(LIBDIR)
	-install -m644 templates/*  $(CONFDIR)/templates
	-install -m644 resources/*  $(LIBDIR)/resources

install-daemon: install
	install -d $(CONFDIR) $(INITDIR)
	if [ ! -e $(CONFDIR)/edna.conf ] ; then install edna.conf $(CONFDIR) ; fi
	install daemon/edna $(INITDIR)/edna

clean:
	rm -f *~ *.pyc

