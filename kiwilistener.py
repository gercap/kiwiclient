#!/usr/bin/env python
## -*- python -*-

import array, codecs, logging, os, struct, sys, time, traceback, copy, threading, os, socket
from optparse import OptionParser

import kiwiclient
from kiwiworker import KiwiWorker

import pyaudio, wave, pygame
import threading
import numpy as np

class KiwiSoundRecorder(kiwiclient.KiwiSDRStream):
    def __init__(self, options):
        super(KiwiSoundRecorder, self).__init__()
        self._options = options
        self._isWF = False
        freq = options.frequency
        #print "%s:%s freq=%d" % (options.server_host, options.server_port, freq)
        self._freq = freq
        self._start_ts = None
        self._start_time = None
        self._squelch_on_seq = None
        self._nf_array = array.array('i')
        for x in range(65):
            self._nf_array.insert(x, 0)
        self._nf_samples = 0
        self._nf_index = 0
        self._num_channels = 2 if options.modulation == 'iq' else 1
        self._last_gps = dict(zip(['last_gps_solution', 'dummy', 'gpssec', 'gpsnsec'], [0,0,0,0]))
        self.local_server_port = options.local_server_port
        self._udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _setup_rx_params(self):
        mod    = self._options.modulation
        if mod == 'usb':
            lp_cut = self._options.lp_cut
            hp_cut = self._options.hp_cut
        elif mod == 'lsb':
            lp_cut = -self._options.hp_cut
            hp_cut = -self._options.lp_cut
        elif mod == 'am':
            # For AM, ignore the low pass filter cutoff
            lp_cut = -hp_cut
        self.set_mod(mod, lp_cut, hp_cut, self._freq)
        if self._options.agc_gain != None:
            self.set_agc(on=False, gain=self._options.agc_gain)
        else:
            self.set_agc(on=True)
        if self._options.compression is False:
            self._set_snd_comp(False)
        self.set_inactivity_timeout(0)
        self.set_name(self._options.user)

    def _process_audio_samples(self, seq, samples, rssi):
        sys.stdout.write('\rBlock: %08x, RSSI: %-04d' % (seq, rssi))
        data = np.array(samples, np.int16)
        audio_channel.queue(pygame.sndarray.make_sound(data))
        self._udp_socket.sendto(samples, ("127.0.0.1", self.local_server_port))

        #samples.tofile(sys.stdout)

class KiwiWaterfallRecorder(kiwiclient.KiwiSDRStream):
    def __init__(self, options):
        super(KiwiWaterfallRecorder, self).__init__()
        self._options = options
        self._isWF = True
        freq = options.frequency
        #print "%s:%s freq=%d" % (options.server_host, options.server_port, freq)
        self._freq = freq
        self._start_ts = None

        # xxx
        self._squelch_on_seq = None
        self._nf_array = array.array('i')
        for x in range(65):
            self._nf_array.insert(x, 0)
        self._nf_samples = 0
        self._nf_index = 0
        self._num_channels = 2 if options.modulation == 'iq' else 1
        self._last_gps = dict(zip(['last_gps_solution', 'dummy', 'gpssec', 'gpsnsec'], [0,0,0,0]))

    def _setup_rx_params(self):
        self._set_zoom_start(0, 0)
        self._set_maxdb_mindb(-10, -110)    # needed, but values don't matter
        #self._set_wf_comp(True)
        self._set_wf_comp(False)
        self._set_wf_speed(1)   # 1 Hz update
        self.set_inactivity_timeout(0)
        self.set_name(self._options.user)

    def _process_waterfall_samples(self, seq, samples):
        nbins = len(samples)
        bins = nbins-1
        max = -1
        min = 256
        bmax = bmin = 0
        i = 0
        for s in samples:
            if s > max:
                max = s
                bmax = i
            if s < min:
                min = s
                bmin = i
            i += 1
        span = 30000
        print("wf samples %d bins %d..%d dB %.1f..%.1f kHz rbw %d kHz"
              % (nbins, min-255, max-255, span*bmin/bins, span*bmax/bins, span/bins))

def options_cross_product(options):
    """build a list of options according to the number of servers specified"""
    def _sel_entry(i, l):
        """if l is a list, return the element with index i, else return l"""
        return l[min(i, len(l)-1)] if type(l) == list else l

    l = []
    for i,s in enumerate(options.server_host):
        opt_single = copy.copy(options)
        opt_single.server_host = s;
        opt_single.status = 0;
        for x in ['server_port', 'password', 'frequency', 'agc_gain', 'filename', 'station', 'user']:
            opt_single.__dict__[x] = _sel_entry(i, opt_single.__dict__[x])
        l.append(opt_single)
    return l

def get_comma_separated_args(option, opt, value, parser, fn):
    values = [fn(v.strip()) for v in value.split(',')]
    setattr(parser.values, option.dest, values)
##    setattr(parser.values, option.dest, map(fn, value.split(',')))

def join_threads(snd, wf):
    [r._event.set() for r in snd]
    [r._event.set() for r in wf]
    [t.join() for t in threading.enumerate() if t is not threading.currentThread()]

def main():
    global keep_running
##    sys.stdout = codecs.getwriter('utf-8')(sys.stdout)

    parser = OptionParser()
    parser.add_option('--log-level', '--log_level', type='choice',
                      dest='log_level', default='warn',
                      choices=['debug', 'info', 'warn', 'error', 'critical'],
                      help='Log level: debug|info|warn|error|critical')
    parser.add_option('-q', '--quiet',
                      dest='quiet',
                      default=False,
                      action='store_true',
                      help='Don\'t print progress messages')
    parser.add_option('-k', '--socket-timeout', '--socket_timeout',
                      dest='socket_timeout', type='int', default=10,
                      help='Timeout(sec) for sockets')
    parser.add_option('-s', '--server-host',
                      dest='server_host', type='string',
                      default='localhost', help='Server host (can be a comma-delimited list)',
                      action='callback',
                      callback_args=(str,),
                      callback=get_comma_separated_args)
    parser.add_option('-p', '--server-port',
                      dest='server_port', type='string',
                      default=8073, help='Server port, default 8073 (can be a comma delimited list)',
                      action='callback',
                      callback_args=(int,),
                      callback=get_comma_separated_args)
    parser.add_option('--pw', '--password',
                      dest='password', type='string', default='',
                      help='Kiwi login password (if required, can be a comma delimited list)',
                      action='callback',
                      callback_args=(str,),
                      callback=get_comma_separated_args)
    parser.add_option('-u', '--user',
                      dest='user', type='string', default='kiwirecorder.py',
                      help='Kiwi connection user name',
                      action='callback',
                      callback_args=(str,),
                      callback=get_comma_separated_args)
    parser.add_option('-f', '--freq',
                      dest='frequency',
                      type='string', default=1000,
                      help='Frequency to tune to, in kHz (can be a comma-separated list)',
                      action='callback',
                      callback_args=(float,),
                      callback=get_comma_separated_args)
    parser.add_option('-m', '--modulation',
                      dest='modulation',
                      type='string', default='lsb',
                      help='Modulation; one of am, lsb, usb, cw, nbfm, iq')
    parser.add_option('--ncomp', '--no_compression',
                      dest='compression',
                      default=True,
                      action='store_false',
                      help='Don\'t use audio compression')
    parser.add_option('--dt-sec',
                      dest='dt',
                      type='int', default=0,
                      help='Start a new file when mod(sec_of_day,dt) == 0')
    parser.add_option('-L', '--lp-cutoff',
                      dest='lp_cut',
                      type='float', default=100,
                      help='Low-pass cutoff frequency, in Hz')
    parser.add_option('-H', '--hp-cutoff',
                      dest='hp_cut',
                      type='float', default=2600,
                      help='Low-pass cutoff frequency, in Hz')
    parser.add_option('--fn', '--filename',
                      dest='filename',
                      type='string', default='',
                      help='Use fixed filename instead of generated filenames (optional station ID(s) will apply)',
                      action='callback',
                      callback_args=(str,),
                      callback=get_comma_separated_args)
    parser.add_option('--station',
                      dest='station',
                      type='string', default=None,
                      help='Station ID to be appended (can be a comma-separated list)',
                      action='callback',
                      callback_args=(str,),
                      callback=get_comma_separated_args)
    parser.add_option('-d', '--dir',
                      dest='dir',
                      type='string', default=None,
                      help='Optional destination directory for files')
    parser.add_option('-w', '--kiwi-wav',
                      dest='is_kiwi_wav',
                      default=False,
                      action='store_true',
                      help='Use wav file format including KIWI header (GPS time-stamps) only for IQ mode')
    parser.add_option('--kiwi-tdoa',
                      dest='is_kiwi_tdoa',
                      default=False,
                      action='store_true',
                      help='Used when called by Kiwi TDoA extension')
    parser.add_option('--tlimit', '--time-limit',
                      dest='tlimit',
                      type='float', default=None,
                      help='Record time limit in seconds')
    parser.add_option('-T', '--threshold',
                      dest='thresh',
                      type='float', default=None,
                      help='Squelch threshold, in dB.')
    parser.add_option('-g', '--agc-gain',
                      dest='agc_gain',
                      type='string',
                      default=None,
                      help='AGC gain; if set, AGC is turned off (can be a comma-separated list)',
                      action='callback',
                      callback_args=(float,),
                      callback=get_comma_separated_args)
    parser.add_option('-z', '--zoom',
                      dest='zoom', type='int', default=0,
                      help='Zoom level 0-14')
    parser.add_option('--wf',
                      dest='waterfall',
                      default=False,
                      action='store_true',
                      help='Process waterfall data instead of audio')
    parser.add_option('--snd',
                      dest='sound',
                      default=False,
                      action='store_true',
                      help='Also process sound data when in waterfall mode')

    parser.add_option('-t', '--local-port',
                      dest='local_server_port', type='int',
                      default=10001, help='Local Server port, default 10001')

    (options, unused_args) = parser.parse_args()
    
    logging.basicConfig(level=logging.getLevelName(options.log_level.upper()))

    run_event = threading.Event()
    run_event.set()

    gopt = options
    options = options_cross_product(options)

    snd_recorders = []
    if not gopt.waterfall or (gopt.waterfall and gopt.sound):
        for i,opt in enumerate(options):
            opt.idx = i
            snd_recorders.append(KiwiWorker(args=(KiwiSoundRecorder(opt),opt,run_event)))

    wf_recorders = []
    if gopt.waterfall:
        for i,opt in enumerate(options):
            wf_recorders.append(KiwiWorker(args=(KiwiWaterfallRecorder(opt),opt,run_event)))

    try:
        for i,r in enumerate(snd_recorders):
            if i!=0 and options[i-1].server_host == options[i].server_host:
                time.sleep(1)
            r.start()
            logging.info("started sound recorder %d" % i)

        for i,r in enumerate(wf_recorders):
            if i!=0 and options[i-1].server_host == options[i].server_host:
                time.sleep(1)
            r.start()
            logging.info("started waterfall recorder %d" % i)

        while run_event.is_set():
            time.sleep(.1)
    except KeyboardInterrupt:
        keep_running = False
        run_event.clear()
        join_threads(snd_recorders, wf_recorders)
        print("KeyboardInterrupt: threads successfully closed")
    except Exception as e:
        traceback.print_exc()
        run_event.clear()
        join_threads(snd_recorders, wf_recorders)
        print("Exception: threads successfully closed")

    if gopt.is_kiwi_tdoa:
      for i,opt in enumerate(options):
          print("status=%d,%d" % (i, opt.status))

if __name__ == '__main__':


    pygame.mixer.init(12000, 16, 1, 1024)
    audio_channel = pygame.mixer.Channel(0)

    keep_running = True

    main()

# EOF
