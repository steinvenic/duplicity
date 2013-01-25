# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2002 Ben Escoto <ben@emerose.org>
# Copyright 2007 Kenneth Loafman <kenneth@loafman.com>
#
# This file is part of duplicity.
#
# Duplicity is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# Duplicity is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with duplicity; if not, write to the Free Software Foundation,
# Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA
#
# @author: Juan Antonio Moya Vicen <juan@nowcomputing.com>
#
"""
Functions to compute progress of compress & upload files
The heuristics try to infer the ratio between the amount of data collected
by the deltas and the total size of the changing files. It also infers the
compression and encryption ration of the raw deltas before sending them to
the backend.
With the inferred ratios, the heuristics estimate the percentage of completion
and the time left to transfer all the (yet unknown) amount of data to send.
This is a forecast based on gathered evidence.
"""


import math
import threading
import time
from datetime import datetime, timedelta
from duplicity import globals
from duplicity import log

def import_non_local(name, custom_name=None):
    """
    This function is needed to play a trick... as there exists a local
    "collections" module, that is named the same as a system module
    """
    import imp, sys

    custom_name = custom_name or name

    f, pathname, desc = imp.find_module(name, sys.path[1:])
    module = imp.load_module(custom_name, f, pathname, desc)
    f.close()

    return module

"""
Import non-local module, use a custom name to differentiate it from local
This name is only used internally for identifying the module. We decide
the name in the local scope by assigning it to the variable sys_collections.
"""
sys_collections = import_non_local('collections','sys_collections')



tracker = None
progress_thread = None

class ProgressTracker():
    
    def __init__(self):
        self.total_stats = None
        self.nsteps = 0
        self.start_time = None
        self.change_mean_ratio = 0.0
        self.change_r_estimation = 0.0
        self.compress_mean_ratio = 0.0
        self.compress_r_estimation = 0.0
        self.progress_estimation = 0.0
        self.time_estimation = 0
        self.total_bytecount = 0
        self.last_total_bytecount = 0
        self.last_bytecount = 0
        self.stall_last_time = None
        self.last_time = None
        self.elapsed_sum = timedelta()
        self.speed = 0.0
        self.transfers = sys_collections.deque()
    
    def has_collected_evidence(self):
        """
        Returns true if the progress computation is on and duplicity has not
        yet started the first dry-run pass to collect some information
        """
        return (not self.total_stats is None)
    
    def log_upload_progress(self):
        """
        Aproximative and evolving method of computing the progress of upload
        """
        if not globals.progress or not self.has_collected_evidence():
            return

        current_time = datetime.now()
        if self.start_time is None:
            self.start_time = current_time
        if not self.last_time is None:
            elapsed = (current_time - self.last_time)
        else:
            elapsed = timedelta()
        self.last_time = current_time
    
        # Detect (and report) a stallment if no changing data for more than 5 seconds
        if self.stall_last_time is None:
            self.stall_last_time = current_time
        if (current_time - self.stall_last_time).seconds > max(5, 2 * globals.progress_rate):
            log.TransferProgress(100.0 * self.progress_estimation, 
                                    self.time_estimation, self.total_bytecount, 
                                    (current_time - self.start_time).seconds,
                                    self.speed, 
                                    True
                                )
            return
    
        self.nsteps += 1
    
        """
        Compute the ratio of information being written for deltas vs file sizes
        Using Knuth algorithm to estimate approximate upper bound in % of completion
        The progress is estimated on the current bytes written vs the total bytes to
        change as estimated by a first-dry-run. The weight is the ratio of changing 
        data (Delta) against the total file sizes. (pessimistic estimation)
        """
        from duplicity import diffdir
        changes = diffdir.stats.NewFileSize + diffdir.stats.ChangedFileSize
        total_changes = self.total_stats.NewFileSize + self.total_stats.ChangedFileSize
        if changes == 0 or total_changes == 0:
            return
    
        # Snapshot current values for progress
        last_progress_estimation = self.progress_estimation
        
        # Compute ratio of changes
        change_ratio = diffdir.stats.RawDeltaSize / float(changes)
        change_delta = change_ratio - self.change_mean_ratio
        self.change_mean_ratio += change_delta / float(self.nsteps) # mean cumulated ratio
        self.change_r_estimation += change_delta * (change_ratio - self.change_mean_ratio)
        change_sigma = math.sqrt(math.fabs(self.change_r_estimation / float(self.nsteps)))
    
        # Compute ratio of compression of the deltas
        compress_ratio = self.total_bytecount / float(diffdir.stats.RawDeltaSize)
        compress_delta = compress_ratio - self.compress_mean_ratio
        self.compress_mean_ratio += compress_delta / float(self.nsteps) # mean cumulated ratio
        self.compress_r_estimation += compress_delta * (compress_ratio - self.compress_mean_ratio)
        compress_sigma = math.sqrt(math.fabs(self.compress_r_estimation / float(self.nsteps)))
    
        # Combine 2 statistically independent variables (ratios) optimistically
        self.progress_estimation = (self.change_mean_ratio * self.compress_mean_ratio 
                                        + change_sigma + compress_sigma) * float(changes) / float(total_changes)
        self.progress_estimation = max(0.0, min(self.progress_estimation, 1.0))
    

        """
        Estimate the time just as a projection of the remaining time, fit to a [(1 - x) / x] curve
        """
        self.elapsed_sum += elapsed # As sum of timedeltas, so as to avoid clock skew in long runs (adding also microseconds)
        projection = 1.0
        if self.progress_estimation > 0:
            projection = (1.0 - self.progress_estimation) / self.progress_estimation
        if self.elapsed_sum.total_seconds() > 0: 
           self.time_estimation = long(projection * float(self.elapsed_sum.total_seconds()))
    
        # Apply values only when monotonic, so the estimates look more consistent to the human eye
        if self.progress_estimation < last_progress_estimation:
            self.progress_estimation = last_progress_estimation
    
        """
        Compute Exponential Moving Average of speed as bytes/sec of the last 30 probes
        """
        if self.elapsed.total_seconds() > 0: 
            self.transfers.append(float(self.total_bytecount - self.last_total_bytecount) / float(elapsed.total_seconds()))
        self.last_total_bytecount = self.total_bytecount
        if len(self.transfers) > 30:
            self.transfers.popleft()
        self.speed = 0.0
        for x in self.transfers:
            self.speed = 0.3 * x + 0.7 * self.speed

        log.TransferProgress(100.0 * self.progress_estimation, 
                                self.time_estimation, 
                                self.total_bytecount, 
                                (current_time - self.start_time).seconds, 
                                self.speed,
                                False
                            )
    
    
    def annotate_written_bytes(self, bytecount):
        """
        Annotate the number of bytes that have been added/changed since last time
        this function was called.
        bytecount param will show the number of bytes since the start of the current
        volume and for the current volume
        """
        changing = max(bytecount - self.last_bytecount, 0)
        self.total_bytecount += long(changing) # Annotate only changing bytes since last probe
        self.last_bytecount = bytecount
        if changing > 0:
            self.stall_last_time = datetime.now()
    
    def set_evidence(self, stats):
        """
        Stores the collected statistics from a first-pass dry-run, to use this
        information later so as to estimate progress
        """
        self.total_stats = stats
    
    def total_elapsed_seconds(self):
        """
        Elapsed seconds since the first call to log_upload_progress method
        """
        return (datetime.now() - self.start_time).seconds
    

def report_transfer(bytecount, totalbytes):
    """
    Method to call tracker.annotate_written_bytes from outside
    the class, and to offer the "function(long, long)" signature
    which is handy to pass as callback
    """
    global tracker
    global progress_thread
    if not progress_thread is None and not tracker is None:
        tracker.annotate_written_bytes(bytecount)


class LogProgressThread(threading.Thread):
    """
    Background thread that reports progress to the log, 
    every --progress-rate seconds 
    """
    def __init__(self):
        super(LogProgressThread, self).__init__()
        self.setDaemon(True)
        self.finished = False

    def run(self):
        global tracker
        if not globals.dry_run and globals.progress and tracker.has_collected_evidence():
            while not self.finished:
                tracker.log_upload_progress()
                time.sleep(globals.progress_rate)
            log.TransferProgress(100.0, 0, tracker.total_bytecount, tracker.total_elapsed_seconds(), tracker.speed, False)

