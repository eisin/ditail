#!/usr/bin/env python3

__version__ = "0.1.0"

import argparse
import asyncio
import logging
import os
import sys
import re
import datetime

import pyinotify

logger = logging.getLogger("ditail")

@asyncio.coroutine
def tail_task(path, modify_event, loop, disable_truncate_detect, new=False):
    f = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not new:
            # Seek to end of file
            os.lseek(f, 0, os.SEEK_END)

        guard_truncate = False
        line_buffer = bytearray()
        while True:
            block = os.read(f, 100 * 1024 * 1024)
            if block == b'' and guard_truncate:
                # Reset so os.read will return data again
                logger.debug("Truncation detected for {}, resetting".format(path))
                os.lseek(f, 0, os.SEEK_SET)
                block = os.read(f, 100 * 1024 * 1024)
            guard_truncate = False

            if block == b'':
                logger.debug("No more to read for {}, waiting for modify event".format(path))
                yield from modify_event.wait()
                modify_event.clear()
                if not disable_truncate_detect:
                    guard_truncate = True
            else:
                line_buffer.extend(block)
                while True:
                    n = line_buffer.find(b"\n")
                    if n == -1:
                        break
                    else:
                        # Write out through newline, remove that portion from buffer
                        sys.stdout.write(path.encode())
                        sys.stdout.write(b":")
                        sys.stdout.write(line_buffer[:(n+1)])
                        line_buffer = line_buffer[(n+1):]
    finally:
        os.close(f)

def update_tasks_in(directory, tail_tasks, tail_events, loop, filter_regexp, mmin, disable_truncate_detect):
    wm = pyinotify.WatchManager()
    mask = pyinotify.IN_DELETE | pyinotify.IN_CREATE | pyinotify.IN_MODIFY

    class Handler(pyinotify.ProcessEvent):
        def __init__(self, filter_regexp, mmin, disable_truncate_detect):
            self.filter_regexp = filter_regexp
            self.mmin = mmin
            self.disable_truncate_detect = disable_truncate_detect

        def process_IN_CREATE(self, event):
            path = event.pathname
            if os.path.isfile(path):
                if path in tail_tasks:
                    logger.debug("Cancelling existing task for {}".format(path))
                    tail_tasks[path].cancel()
                    del tail_tasks[path]
                if self.filter_regexp:
                    pattern = re.compile(self.filter_regexp)
                if self.filter_regexp and not pattern.search(path):
                    logger.debug("New path {} not matched the filter, ignoring".format(path))
                else:
                    logger.debug("Creating task for {}".format(path))
                    modify_event = asyncio.Event(loop=loop)
                    tail_events[path] = modify_event
                    tail_tasks[path] = loop.create_task(tail_task(path, modify_event, loop, self.disable_truncate_detect, new=True))
            else:
                logger.debug("New path {} is not a file, ignoring".format(path))

        def process_IN_DELETE(self, event):
            path = event.pathname
            if path in tail_tasks:
                logger.debug("Cancelling task for {}".format(path))
                tail_tasks[path].cancel()
                del tail_tasks[path]
            else:
                logger.debug("No task to cancel for {}".format(path))

        def process_IN_MODIFY(self, event):
            path = event.pathname
            if path in tail_events:
                logger.debug("Event set for {}".format(path))
                tail_events[path].set()
            else:
                logger.debug("No event to set for {}".format(path))

    notifier = pyinotify.AsyncioNotifier(wm, loop, default_proc_fun=Handler(filter_regexp, mmin, disable_truncate_detect))
    wm.add_watch(directory, mask, rec=True, auto_add=True)

def generate_files_in(directory, filter_regexp, mmin):
    if filter_regexp:
        pattern = re.compile(filter_regexp)
    for dirpath, dirnames, filenames in os.walk(directory):
        for filename in filenames:
            fullpath = os.path.join(dirpath, filename)
            if filter_regexp and pattern.search(filename):
                continue
            if mmin and not mmin_filter_allow(fullpath, mmin):
                continue
            yield fullpath

def tail_files_in(directory, filter_regexp, mmin, disable_truncate_detect):
    loop = asyncio.get_event_loop()
    tail_tasks = dict()
    tail_events = dict()
    abs_directory = os.path.abspath(directory)

    try:
        for path in generate_files_in(abs_directory, filter_regexp, mmin):
            logger.debug("Creating initial task for {}".format(path))
            modify_event = asyncio.Event(loop=loop)
            tail_events[path] = modify_event
            tail_tasks[path] = loop.create_task(tail_task(path, modify_event, loop, disable_truncate_detect))
        update_tasks_in(abs_directory, tail_tasks, tail_events, loop, filter_regexp, mmin, disable_truncate_detect)
        if mmin and mmin > 0:
            loop.call_soon(watch_mmin_task, loop, tail_tasks, mmin)

        logger.debug("Starting loop")
        loop.run_forever()
        logger.debug("Loop stopped")
    except KeyboardInterrupt:
        logger.debug("Keyboard Interrupt, closing loop")
    finally:
        tasks = asyncio.Task.all_tasks()
        for task in tasks:
            task.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*tasks, loop=loop))
        except asyncio.CancelledError:
            pass
        loop.close()

def watch_mmin_task(loop, tail_tasks, mmin):
    del_tasks = []
    for path in tail_tasks:
        if not mmin_filter_allow(path, mmin):
            logger.debug("Cancelling existing task for {}, because not recently modified".format(path))
            del_tasks.append(path)
    for path in del_tasks:
        tail_tasks[path].cancel()
        del tail_tasks[path]
    loop.call_later(60, watch_mmin_task, loop, tail_tasks, mmin)

def mmin_filter_allow(filename, mmin):
    if mmin == 0:
        return True
    try:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filename))
    except:
        pass
    mtime_diff = datetime.datetime.now() - mtime
    mtime_diff_minutes = int(mtime_diff.total_seconds() / 60)
    if mmin >= mtime_diff_minutes:
        return True
    return False

#
# Main
#

def create_argparser():
    parser = argparse.ArgumentParser(description="Prints new lines added to all files in a directory and its subdirectories. Any files added after startup will also be monitored.")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable logging (at DEBUG level)")
    parser.add_argument("-f", "--filter", action="store", help="Filter filename (regexp)")
    parser.add_argument("-m", "--mmin", action="store", type=int, help="Exclude files not recently modified (in minutes)")
    parser.add_argument("--disable-truncate-detect", action="store_true", help="Disable file truncate detection")
    parser.add_argument("directory", help="The directory to look in for files to tail")
    return parser

def setup_logging():
    logging.basicConfig(level=logging.DEBUG)

def configure_stdio():
    sys.stdout = os.fdopen(sys.stdout.fileno(), "wb", buffering=0)

def main():
    parser = create_argparser()
    args = parser.parse_args()
    if args.debug:
        setup_logging()
    configure_stdio()
    tail_files_in(args.directory, args.filter, args.mmin, args.disable_truncate_detect)

if __name__ == "__main__":
    main()
