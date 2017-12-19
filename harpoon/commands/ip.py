#! /usr/bin/env python
import sys
import os
import json
import datetime
import urllib.request
import tarfile
import geoip2.database
import re
import subprocess
import glob
import shutil
import pyasn
from IPy import IP
from dateutil.parser import parse
from harpoon.commands.base import Command
from harpoon.lib.utils import bracket, unbracket
from harpoon.lib.robtex import Robtex, RobtexError
from OTXv2 import OTXv2, IndicatorTypes
from virus_total_apis import PublicApi, PrivateApi
from greynoise import GreyNoise, GreyNoiseError


class CommandIp(Command):
    name = "ip"
    description = "Gather information on an IP address"
    config = None
    update_needed = True
    geocity = os.path.join(os.path.expanduser('~'), '.config/harpoon/GeoLite2-City.mmdb')
    geoasn = os.path.join(os.path.expanduser('~'), '.config/harpoon/GeoLite2-ASN.mmdb')
    asnname = os.path.join(os.path.expanduser('~'), '.config/harpoon/asnnames.csv')
    asncidr = os.path.join(os.path.expanduser('~'), '.config/harpoon/asncidr.dat')

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(help='Subcommand')
        parser_a = subparsers.add_parser('info', help='Information on an IP')
        parser_a.add_argument('IP', help='IP address')
        parser_a.set_defaults(subcommand='info')
        parser_b = subparsers.add_parser('intel', help='Gather Threat Intelligence information on an IP')
        parser_b.add_argument('IP', help='IP address')
        parser_b.set_defaults(subcommand='intel')
        self.parser = parser

    def update(self):
        # Download Maxmind
        print("Downloading MaxMind GeoIP Database")
        try:
            os.remove(self.geocity)
        except OSError:
            pass
        try:
            os.remove(self.geoasn)
        except OSError:
            pass
        file_name, headers = urllib.request.urlretrieve('http://geolite.maxmind.com/download/geoip/database/GeoLite2-City.tar.gz')
        tar = tarfile.open(file_name, 'r')
        mmdb = tar.extractfile(tar.getmembers()[3])
        with open(self.geocity, 'wb+') as f:
            f.write(mmdb.read())
        mmdb.close()
        print("-GeoLite2-City.mmdb")
        file_name, headers = urllib.request.urlretrieve('http://geolite.maxmind.com/download/geoip/database/GeoLite2-ASN.tar.gz')
        tar = tarfile.open(file_name, 'r')
        mmdb = tar.extractfile(tar.getmembers()[3])
        with open(self.geoasn, 'wb+') as f:
            f.write(mmdb.read())
        mmdb.close()
        print("-GeoLite2-ASN.mmdb")
        print("Download ASN Name database")
        try:
            os.remove(self.asnname)
        except OSError:
            pass
        file_name, headers = urllib.request.urlretrieve('http://www.cidr-report.org/as2.0/autnums.html')
        fin = open(file_name, 'r', encoding="latin-1", errors='ignore')
        fout = open(self.asnname, 'w+')
        line = fin.readline()
        reg = re.compile('^<a href="/cgi-bin/as-report\?as=AS\d+&view=2.0">AS(\d+)\s*</a> (.+)$')
        while line != '':
            res = reg.match(line)
            if res:
                fout.write('%s|%s\n' % (res.group(1), res.group(2)))
            line = fin.readline()
        fin.close()
        fout.close()
        print('-asnname.csv')
        print("Downloading CIDR data")
        try:
            os.remove(self.asncidr)
        except OSError:
            pass
        os.chdir("/tmp")
        subprocess.call(["pyasn_util_download.py", "--latest"])
        ls = glob.glob("rib*.bz2")[0]
        subprocess.call(['pyasn_util_convert.py', '--single', ls, 'latest.dat'])
        shutil.move('latest.dat', self.asncidr)
        print('-asncidr.dat')

    def run(self, conf, args, plugins):
        if 'subcommand' in args:
            if args.subcommand == 'info':
                # FIXME: move code here in a library
                ip = unbracket(args.IP)
                try:
                    ipy = IP(ip)
                except ValueError:
                    print('Invalid IP format, quitting...')
                    return
                try:
                    citydb = geoip2.database.Reader(self.geocity)
                    res = citydb.city(ip)
                    print('MaxMind: Located in %s, %s' % (
                            res.city.name,
                            res.country.name
                        )
                    )
                except geoip2.errors.AddressNotFoundError:
                    print("MaxMind: IP not found in the city database")
                try:
                    asndb = geoip2.database.Reader(self.geoasn)
                    res = asndb.asn(ip)
                    print('MaxMind: ASN%i, %s' % (
                            res.autonomous_system_number,
                            res.autonomous_system_organization
                        )
                    )
                except geoip2.errors.AddressNotFoundError:
                    print("MaxMind: IP not found in the ASN database")
                asndb2 = pyasn.pyasn(self.asncidr)
                res = asndb2.lookup(ip)
                if res[1] is None:
                    print("IP not found in ASN database")
                else:
                    # Search for name
                    f = open(self.asnname, 'r')
                    found = False
                    line = f.readline()
                    name = ''
                    while not found and line != '':
                        s = line.split('|')
                        if s[0] == str(res[0]):
                            name = s[1].strip()
                            found = True
                        line = f.readline()

                    print('ASN %i - %s (range %s)' % (
                            res[0],
                            name,
                            res[1]
                        )
                    )
                print("")
                if ipy.iptype() == "PRIVATE":
                    "Private IP"
                if ipy.version() == 4:
                    print("Censys:\t\thttps://censys.io/ipv4/%s" % ip)
                    print("Shodan:\t\thttps://www.shodan.io/host/%s" % ip)
                    print("IP Info:\thttp://ipinfo.io/%s" % ip)
                    print("BGP HE:\t\thttps://bgp.he.net/ip/%s" % ip)
                    print("IP Location:\thttps://www.iplocation.net/?query=%s" % ip)
            elif args.subcommand == "intel":
                # Start with MISP and OTX to get Intelligence Reports
                print('###################### %s ###################' % args.IP)
                passive_dns = []
                urls = []
                malware = []
                files = []
                # OTX
                otx_e = plugins['otx'].test_config(conf)
                if otx_e:
                    print('[+] Downloading OTX information....')
                    otx = OTXv2(conf["AlienVaultOtx"]["key"])
                    res = otx.get_indicator_details_full(IndicatorTypes.IPv4, args.IP)
                    otx_pulses =  res["general"]["pulse_info"]["pulses"]
                    # Get Passive DNS
                    if "passive_dns" in res:
                        for r in res["passive_dns"]["passive_dns"]:
                            passive_dns.append({
                                "domain": r['hostname'],
                                "first": parse(r["first"]),
                                "last": parse(r["last"]),
                                "source" : "OTX"
                            })
                    if "url_list" in res:
                        for r in res["url_list"]["url_list"]:
                            urls.append(r)
                # RobTex
                print('[+] Downloading Robtex information....')
                rob = Robtex()
                res = rob.get_ip_info(args.IP)
                for d in ["pas", "pash", "act", "acth"]:
                    if d in res:
                        for a in res[d]:
                            passive_dns.append({
                                'first': a['date'],
                                'last': a['date'],
                                'domain': a['o'],
                                'source': 'Robtex'
                            })
                # VT
                vt_e = plugins['vt'].test_config(conf)
                if vt_e:
                    if conf["VirusTotal"]["type"] != "public":
                        print('[+] Downloading VT information....')
                        vt = PrivateApi(conf["VirusTotal"]["key"])
                        res = vt.get_ip_report(args.IP)
                        if "results" in res:
                            if "resolutions" in res['results']:
                                for r in res["results"]["resolutions"]:
                                    passive_dns.append({
                                        "first": parse(r["last_resolved"]),
                                        "last": parse(r["last_resolved"]),
                                        "domain": r["hostname"],
                                        "source": "VT"
                                    })
                            if "undetected_downloaded_samples" in res['results']:
                                for r in res['results']['undetected_downloaded_samples']:
                                    files.append({
                                        'hash': r['sha256'],
                                        'date': parse(r['date']),
                                        'source' : 'VT'
                                    })
                            if "undetected_referrer_samples" in res['results']:
                                for r in res['results']['undetected_referrer_samples']:
                                    files.append({
                                        'hash': r['sha256'],
                                        'date': parse(r['date']),
                                        'source' : 'VT'
                                    })
                            if "detected_downloaded_samples" in res['results']:
                                for r in res['results']['detected_downloaded_samples']:
                                    malware.append({
                                        'hash': r['sha256'],
                                        'date': parse(r['date']),
                                        'source' : 'VT'
                                    })
                            if "detected_referrer_samples" in res['results']:
                                for r in res['results']['detected_referrer_samples']:
                                    malware.append({
                                        'hash': r['sha256'],
                                        'date': parse(r['date']),
                                        'source' : 'VT'
                                    })
                    else:
                        vt_e = False

                print('[+] Downloading GreyNoise information....')
                gn = GreyNoise()
                try:
                    greynoise = gn.query_ip(args.IP)
                except GreyNoiseError:
                    greynoise = []


                # TODO: MISP
                print('----------------- Intelligence Report')
                if otx_e:
                    if len(otx_pulses) > 0:
                        print('OTX: Found in %i pulses:' % len(otx_pulses))
                        for p in otx_pulses:
                            print('\t %s (%s - %s)' % (
                                    p['name'],
                                    p['created'],
                                    p['id']
                                )
                            )
                    else:
                        print('OTX: Not found in any pulse')
                if len(greynoise) > 0:
                    print("GreyNoise: IP identified as")
                    for r in greynoise:
                        print("\t%s (%s -> %s)" % (
                                r["name"],
                                r["first_seen"],
                                r["last_updated"]
                            )
                        )
                else:
                    print("GreyNoise: Not found")
                if len(malware) > 0:
                    print('----------------- Malware')
                    for r in sorted(malware, key=lambda x: x["date"]):
                        print("[%s] %s %s" % (
                                r["source"],
                                r["hash"],
                                r["date"].strftime("%Y-%m-%d")
                            )
                        )
                if len(files) > 0:
                    print('----------------- Files')
                    for r in sorted(files, key=lambda x: x["date"]):
                        print("[%s] %s %s" % (
                                r["source"],
                                r["hash"],
                                r["date"].strftime("%Y-%m-%d")
                            )
                        )
                if len(passive_dns) > 0:
                    print('----------------- Passive DNS')
                    for r in sorted(passive_dns, key=lambda x: x["first"]):
                        print("[+] %-40s (%s -> %s)(%s)" % (
                                r["domain"],
                                r["first"].strftime("%Y-%m-%d"),
                                r["last"].strftime("%Y-%m-%d"),
                                r["source"]
                            )
                        )


            else:
                self.parser.print_help()
        else:
            self.parser.print_help()
