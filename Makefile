# 
# Makefile to install edna
#

LIBDIR=$(DESTDIR)/usr/lib/edna
BINDIR=$(DESTDIR)/usr/bin
INITDIR=$(DESTDIR)/etc/init.d
CONFDIR=$(DESTDIR)/etc/edna

all:
	@echo "Please adjust values in edna.conf and use" 
	@echo "make install"
	@echo "or make install-daemon"


install:  
	install -d $(BINDIR) $(LIBDIR) $(LIBDIR)/templates
	install edna.py $(BINDIR)/edna
	install ezt.py $(LIBDIR)
	install MP3Info.py $(LIBDIR)
	install -m644 templates/*  $(LIBDIR)/templates

install-daemon: install
	install -d $(CONFDIR) $(INITDIR)
	install edna.conf $(CONFDIR)
	install daemon/edna $(INITDIR)/edna

clean:
	rm -f *~ *.pyc


