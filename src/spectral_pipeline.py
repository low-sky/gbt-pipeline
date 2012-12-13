import fitsio
import numpy as np
import pylab
from matplotlib.font_manager import FontProperties
from mpl_toolkits.axes_grid.anchored_artists import AnchoredText
import scipy
from scipy import constants
from scipy import signal
import sys
from ordereddict import OrderedDict
import os

import spectral_commandline

ETA_L = .99  # spillover and ohmic loss efficiency factor
ETA_A = .71  # aperture efficiency
TAU = .008   # this should come from the DB, but .008 is for < 2 GHz
FFT_TSYS_THRESHOLD = 20

# limits for masking out galactic hydrogen emmision
VEL_MASK_LO = -300 # km/s
VEL_MASK_HI =  300 # km/s

# We choose to mask a certain percentage of the spectrum b/c of edge effects
PCT_TO_EXCLUDE = .05

# The smoothed spectrum is subtracted from the unsmoothed spectrum to
#  remove narrow band RFI spikes.  This is the size of the smoothing kernel.
SMOOTHING_WINDOW = 5

# When subtracting a baseline, we use the following order.
BASELINE_ORDER = 3

DEBUG = False

POL ={  -1:'RR',-2:'LL',
        -3:'RL',-4:'LR',
        -5:'XX',-6:'YY',-7:'XY',-8:'YX',
         1:'I',  2:'Q',  3:'U',  4:'V'}
    
def polnum2char(num):
    return POL[num]

def flag_rfi(spectrum, niter=10, nsigma=3, filtwin=15):
    """
    flag channels with narrow band RFI
    
    For niter iterations, smooth the spectrum with a window size filtwin.
    On each iteration, flag any channels that are nsigma standard deviations
    above the mean.
    
    This should eliminate most narrow band RFI.
    """
    sig = np.ma.array(spectrum)
    sig_smoothed = signal.medfilt(sig,filtwin)

    while niter>0:

        sd = (sig-sig_smoothed).std()
        spikes = abs(sig-sig_smoothed)
        mask = (spikes > (nsigma*sd)).data 
        sig.mask = np.logical_or( sig.mask, mask)
        niter -= 1
    
    return sig.mask

def freq_axis(data,verbose=0):
    """ frequency axis to return for plotting

    Keyword arguments:
    data
    
    Returns:
    A frequency axis vector for the scan.
    """
    
    # apply sampler filter
    crpix1 = data['CRPIX1']
    cdelt1 = data['CDELT1']
    crval1 = data['CRVAL1']

    faxis = np.zeros(len(data['DATA']))
    for chan,ee in enumerate(data['DATA']):
        faxis[chan] = (chan-crpix1) * cdelt1 + crval1

    return faxis

# Convert frequency to velocity (m/s) using the given rest
# frequency and velocity definition.  The units (Hz, MHz, GHz, etc)
# of the frequencies to convert must match that of the rest frequency 
# argument.
#
# @param freq {in}{required} Frequency. Units must be the same as 
# the units of restfreq.
# @param restfreq {in}{required} Rest frequency.  Units must be the
# same as those of freq.
# @keyword veldef {in}{optional}{type=string} The velocity definition
# which must be one of OPTICAL, RADIO, or TRUE.  Defaults to RADIO.
#
# @returns velocity in m/s
def freqtovel(freq, restfreq, veldef='RADIO'):

    LIGHT_SPEED = constants.c/1e3 # km/s
    freq = float(freq)
    restfreq = float(restfreq)
    
    #print '[{vd}]'.format(vd=veldef)
    if veldef.startswith('RADI'):
        result = LIGHT_SPEED * ((restfreq - freq) / restfreq)
    elif veldef.startswith('OPTI'):
        result = LIGHT_SPEED * (restfreq / (freq - restfreq))
    elif veldef.startswith('RELA'):
        gg = (freq / restfreq)**2
        result = LIGHT_SPEED * ((restfreq - gg) / (restfreq + gg))
    else:
        print 'unrecognized velocity definition'

    return result

def fit_baseline(ydata, order):
    """
    Fit a n-th order baseline to the data and return the fit.
    
    """
    # make a copy of the data mask so we can reset it later
    oldmask = ydata.mask.copy()
    
    datalen = len(ydata)
    xdata = np.linspace(0, datalen, datalen)
    
    # mask out the low and high 10% of the spectrum because the edges
    #  are sometimes bad.  Also, mask out the center 20% of the band because
    #  it is likely to contain a source line
    ydata.mask[:.1*datalen]=True
    ydata.mask[.9*datalen:]=True
    ydata.mask[.4*datalen:.6*datalen]=True
    
    xdata = np.ma.array(xdata)
    xdata.mask = ydata.mask
    
    # do the fit
    polycoeffs = np.ma.polyfit(xdata, ydata, order)
    yfit = scipy.polyval(polycoeffs, xdata)
    yfit = np.ma.array(yfit)
    
    # reset the mask on ydata
    ydata.mask = oldmask
    
    # return the fit
    return yfit

def rebin_1d(data,binsize):
    rebinned = (data.reshape(len(data)/binsize,binsize)).mean(1)
    return rebinned

def smooth_hanning(data,window_len):
    kernel = signal.hanning(window_len)/2.
    if data.ndim == 2:
        smoothed_data = np.convolve(kernel,data[0],mode='same')
        for spectrum in data[1:]:
            smoothed = np.convolve(kernel,spectrum,mode='same')
            smoothed_data = np.vstack((smoothed_data,spectrum))
    elif data.ndim == 1:
        smoothed_data = np.convolve(kernel,data,mode='same')
    else:
        print 'number of dimensions',data.ndim,'not supported by hanning'
    return smoothed_data

def median(data,window_len):
    return signal.medfilt(data,window_len)

def boxcar(data,window_len):
    kernel = signal.boxcar(window_len)/float(window_len)
    smoothed_data = np.convolve(kernel,data,mode='same')
    return smoothed_data

def mask_data(data, maskargs):
    """
    Return the masked data
    
    """
    mymaskeddata = data[ domask(data, maskargs) ]
    return mymaskeddata 

def domask(data, margs):
    """
    Return the mask based on the arguments
    
    """
    key = margs.keys()[0]
    
    if np.ndarray == type(data):
        thismask = ( data[key] == margs[key] )
        return thismask
    
    else:
        # if type is int
        if type(1) == type(margs[key]) or np.int16 == type(margs[key]):
            retval = data.where(key + ' == ' + str(margs[key]))
        # if type is string
        elif type('') == type(margs[key]):
            retval = data.where(key + ' == \'' + margs[key] + '\'')
        else:
            retval = False
            
    return retval

if __name__ == "__main__":

    
    # create instance of CommandLine object to parse input, then
    # parse all the input parameters and store them as attributes in param structure
    cl = spectral_commandline.CommandLine()
    cl_params = cl.read(sys)
    
    FILENAME = cl_params.infilename
    
    # open the input file and get a file handle
    try:
        raw = fitsio.FITS(FILENAME)
    except (ValueError, IOError), ee:
        print ee
        sys.exit()
    
    # create the output file
    outfilename = os.path.basename(FILENAME)+'.reduced.fits'
    sdfits = fitsio.FITS(outfilename, 'rw', clobber = True)
        
    for EXTENSION in range(len(raw)):
    
        if 'SINGLE DISH' != raw[EXTENSION].get_extname():
            continue

        # get a list of target names from the input file
        target_list = [xx.strip() for xx in set(raw[EXTENSION]['OBJECT'][:])]
    
        # for each target, get a pointer to that subset of data
        targets = {}
        for target_name in target_list:
            mask = raw[EXTENSION].where('OBJECT == \'' + target_name + '\'')
            targets[target_name] = raw[EXTENSION][mask]
    
        num = 1
        
        # recast the target data into an ordered dictionary
        targets = OrderedDict(sorted(targets.items(), key=lambda t: t[0]))
         
    
        # get the dtype from an input row to have the right column structure
        #  in the output file
        dtype = raw[EXTENSION][0].dtype
        raw_header = fitsio.read_header(FILENAME, EXTENSION)
        sdfits.create_table_hdu(dtype = dtype)
    
        numtargets = 0
        
        # for each target
        for target_id in targets.keys():
    
            print 'target',target_id,len(targets[target_id]),'integrations'
            
            target_data = targets[target_id]
    
            obsmodes = {}
            # for each scan on the target, collect obsmodes
            for scan in set(target_data['SCAN']):
                obsmodes[str(scan)] = \
                    target_data['OBSMODE'][target_data['SCAN']==scan][0].strip()
    
            obsmodes = OrderedDict(sorted(obsmodes.items(), key=lambda t: t[0]))
         
            # for each scan on the target, make pairs
            scan_pairs = []
            for scan in obsmodes.keys():
                scanmask = target_data['SCAN']==int(scan)
                # check procsize and procseqn
                if (target_data['PROCSIZE'][scanmask][0]) == 2 and \
                    (target_data['PROCSEQN'][scanmask][0]) == 1 and \
                    ('OnOff' in (target_data['OBSMODE'][scanmask][0]) or \
                     'OffOn' in (target_data['OBSMODE'][scanmask][0])):
                
                    if obsmodes.has_key(str(int(scan)+1)):
                        scan_pairs.append( (int(scan),int(scan)+1) )
            
            # if there are no scan pairs, skip to the next target
            if 0 == len(scan_pairs):
                continue
            else:
                numtargets += 1
            
            print 'scans for target',target_id,scan_pairs
            
            final_spectrum = None

            # total duration of time spent on target, to be written in
            #  output header
            target_duration = 0
            # total exposure of time spent on target, to be written in
            #  output header
            target_exposure = 0
            
            weights = []
            tcals = []
            tsyss = []
            
            for pair in scan_pairs:
    
                print 'processing scans',pair
                
                if 'OnOff' in obsmodes[str(pair[0])]:
                    TargScanNum = pair[0]
                    RefScanNum = pair[1]
                elif 'OffOn' in obsmodes[str(pair[0])]:
                    TargScanNum = pair[1]
                    RefScanNum = pair[0]
                else:
                    print 'Error: Unknown OBSMODE',obsmodes[str(pair[0])]
                    continue
            
                # L(ON) - L(OFF) / L(OFF)
                TargDataPols = mask_data(raw[EXTENSION], {'SCAN':TargScanNum})
                RefDataPols = mask_data(raw[EXTENSION], {'SCAN':RefScanNum})
                if len(RefDataPols) != len(TargDataPols):
                    print 'WARNING: Target and Reference scans do not have the '
                    print '         same number of integrations.  Skipping.'
                    continue
                    
                polarizations = set(TargDataPols['CRVAL4'])
    
                for pol in polarizations:
                    print 'polarization',polnum2char(pol)               
    
                    # data object for Target and Reference scans
                    TargData = mask_data(TargDataPols, {'CRVAL4':pol})
                    RefData = mask_data(RefDataPols, {'CRVAL4':pol})
                    
                    # data object for 
                    # calON and calOFF sets os integrations for Target
                    TargOn = mask_data(TargData,{ 'CAL':'T' })
                    TargOff = mask_data(TargData,{ 'CAL':'F' })
                    
                    # get the cumulative duration for the scanpair
                    scanpair_duration = TargOn['DURATION'].sum()
                    # calculation the total duration on this target
                    target_duration += scanpair_duration
                    
                    # get the cumulative exposure for the scanpair
                    scanpair_exposure = TargOn['EXPOSURE'].sum()
                    # calculation the total exposure on this target
                    target_exposure += scanpair_exposure
                    
                    # spectrum for each integration averaged calON and calOFF
                    # for Target
                    TargOnData = smooth_hanning(TargOn['DATA'], SMOOTHING_WINDOW)
                    TargOffData = smooth_hanning(TargOff['DATA'], SMOOTHING_WINDOW)
                    Targ = (TargOnData+TargOffData)/2.
    
                    # data object for 
                    # calON and calOFF sets os integrations for Reference
                    RefOn = mask_data(RefData,{ 'CAL':'T' })
                    RefOff = mask_data(RefData,{ 'CAL':'F' })
                    # spectrum for each integration averaged calON and calOFF
                    # for Reference
                    RefOnData = smooth_hanning(RefOn['DATA'], SMOOTHING_WINDOW)
                    RefOffData = smooth_hanning(RefOff['DATA'], SMOOTHING_WINDOW)
                    Ref = (RefOnData+RefOffData)/2.
                    
                    # flag channels with narrow band RFI
                    # This produces a single RFI mask, to be applied later
                    targ_rfi_mask = flag_rfi(Targ.mean(0))
                    ref_rfi_mask = flag_rfi(Ref.mean(0))
                    rfi_mask = np.logical_or(targ_rfi_mask,ref_rfi_mask)
                    
                    Tcal = RefData['TCAL'].mean()
                    AveRefOff = RefOffData.mean(0)
                    AveRefOn = RefOnData.mean(0)
                    mid80off = AveRefOff[.1*len(AveRefOff):.9*len(AveRefOff)]
                    mid80on = AveRefOn[.1*len(AveRefOn):.9*len(AveRefOn)]
                    
                    Tsys = Tcal * ( mid80off / (mid80on-mid80off) ) + Tcal / 2.
                    Tsys = Tsys.mean()
                    
                    # if Targ and Ref have different numbers of integrations,
                    #   use the lesser number of the two so that each has a
                    #   match and ignore the others
                    maxIntegrations = np.min( (len(Targ),len(Ref)) )
                    Targ = Targ[:maxIntegrations]
                    Ref = Ref[:maxIntegrations]
                    
                    Ta = Tsys * ( (Targ - Ref) / Ref )
                    
                    elevation = TargData['ELEVATIO'].mean()
                    Jy = Ta/2.85 * (np.e**(TAU/np.sin(elevation)))/(ETA_A*ETA_L)
                    
                    cal = Jy
                    
                    cal = np.ma.array(cal)
    
                    freq = freq_axis(TargOn[0])
                    restfreq = TargOn['RESTFREQ'][0]
                    velo = np.array([freqtovel(ff,restfreq) for ff in freq])
    
                    # mask the emission from galactic hydrogen
                    localHImask = np.logical_and( velo > VEL_MASK_LO,
                                                  velo < VEL_MASK_HI )
    
                    velocity_mask = np.array([localHImask] * len(cal))
                    rfimask = np.array([rfi_mask] * len(cal))
                    total_mask = np.logical_or(rfimask,velocity_mask)
    
                    cal_masked = np.ma.masked_array(cal, mask= total_mask)
                    
                    # check baselines of each integration
                    for idx,spec in enumerate(cal_masked):
                    
                        # get the fft of the upper 95% of the spectrum
                        fullfft = np.abs(np.fft.fft(spec[.05*len(spec):]))
                        
                        # top 10% of fft
                        myfft = fullfft[-(.1*len(fullfft)):]
                    
                        #print 'checking integration:', idx
                        
                        # check if all fft vals are within X sigma
                        if myfft.max() > FFT_TSYS_THRESHOLD * Tsys:
                            print 'FLAGGING INTEGRATION',idx
                            cal_masked[idx].mask = True
                            
                        if DEBUG:
                            print 'fft.max()',myfft.max()
    
                    cal_masked.mean(0).mask = flag_rfi(cal_masked.mean(0))
                    vel = velo
                    
                    # if they are all nans, don't attempt to remove baseline
                    if np.all(cal_masked.mean(0).mask):
                        baseline_removed = cal_masked.mean(0)
                    else:
                        yfit = fit_baseline(cal_masked.mean(0), BASELINE_ORDER)
                        yfit.mask = cal_masked.mean(0).mask
        
                        baseline_removed = cal_masked.mean(0)-yfit
    
                    if final_spectrum != None:
                        final_spectrum =\
                            np.ma.vstack((final_spectrum, baseline_removed))
                    else:
                        final_spectrum = baseline_removed
                    
                    # tsys and exposure for this scan pair
                    weight = scanpair_exposure / Tsys**2
                    weights.append(weight)
                    tcals.append(Tcal)
                    tsyss.append(Tsys)
                    
            if final_spectrum != None and final_spectrum.ndim > 1:
                final_spectrum = final_spectrum.mean(0)
    
            final_spectrum = final_spectrum.filled(fill_value=float('nan'))
            
            # set the low and high %5 of the spectrum to nan
            final_spectrum[:PCT_TO_EXCLUDE*len(final_spectrum)] = float('nan')
            final_spectrum[-PCT_TO_EXCLUDE*len(final_spectrum):] = float('nan')
            
            # determine the rms from the low 35% of the band
            #freereg=final_spectrum[:.35*len(final_spectrum)]
            #freereg = np.ma.masked_array(freereg,np.isnan(freereg))
            #rms = np.ma.sqrt((freereg**2).mean())
    
            outputrow = np.zeros(1, dtype=TargData[0].dtype)
    
            for name in sdfits[-1].colnames:
                if type('') == type(TargData[0][name]) or \
                  np.string_ == type(TargData[0][name]):
                    outputrow[name] = TargData[0][name].strip()
                else:
                    outputrow[name] = TargData[0][name]
            
            avg_tsys = np.average(tsyss, axis = 0, weights = weights)            
            outputrow['DURATION'] = target_duration
            outputrow['EXPOSURE'] = target_exposure
            outputrow['DATA'] = final_spectrum
            outputrow['TSYS'] = avg_tsys
            outputrow['TUNIT7'] = 'Jy'  # set units to Janskys

            sdfits[-1].append(outputrow)
            sdfits[-1].write_keys(raw_header, clean=True)
            sdfits.update_hdu_list()
                    
            num += 1
        
    # copy primary header from input to output file
    primary_header = fitsio.read_header(FILENAME, 0)
    sdfits[0].write_keys(primary_header, clean=True)

    sdfits.close()
    raw.close()
