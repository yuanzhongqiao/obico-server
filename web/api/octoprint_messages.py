from django.utils import timezone

from lib import redis
from lib import channels
from lib.utils import set_as_str_if_present
from app.models import PrintEvent

STATUS_TTL_SECONDS = 240

def process_octoprint_status(printer, status):
    octoprint_settings = status.get('octoprint_settings')
    if octoprint_settings:
        redis.printer_settings_set(printer.id, settings_dict(octoprint_settings))

    octoprint_data = dict()
    set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'state')
    set_as_str_if_present(octoprint_data, status.get('octoprint_data', {}), 'progress')
    set_as_str_if_present(octoprint_data, status, 'octoprint_temperatures', 'temperatures')
    redis.printer_status_set(printer.id, octoprint_data, ex=STATUS_TTL_SECONDS)

    if status.get('current_print_ts'): # New plugin version that passes current_print_ts
        process_octoprint_status_with_ts(status, printer)
        channels.send_status_to_web(printer.id)
        return

    ### Old way of determining a print. For backward compatibility
    filename, printing, cancelled = file_printing(status, printer)
    if printing is not None:
        if printing:
            printer.set_current_print(filename)
        else:
            printer.unset_current_print(cancelled)

    channels.send_status_to_web(printer.id)

def file_printing(op_status, printer):
    # Event, if present, should be used to determine the printing status
    op_event = op_status.get('octoprint_event', {})
    filename = (op_event.get('data') or {}).get('name')    # octoprint_event may be {'data': null, xxx}
    if filename and op_event.get('event_type') == 'PrintStarted':
        return filename, True, False
    if filename and op_event.get('event_type') == 'PrintDone':
        return filename, False, False
    if filename and op_event.get('event_type') == 'PrintCancelled':
        return filename, False, True

    # No event. Fall back to using octoprint_data.
    # But we wait for a period because octoprint_data can be out of sync with octoprint_event briefly and cause race condition
    if printer.current_print and (timezone.now() - printer.current_print.updated_at).total_seconds() < 60:
        return None, None, None

    octoprint_data = op_status.get('octoprint_data', {})
    filename = octoprint_data.get('job', {}).get('file', {}).get('name')
    printing = False
    flags = octoprint_data.get('state', {}).get('flags', {})
    for flag in ('cancelling', 'paused', 'pausing', 'printing', 'resuming', 'finishing'):
        if flags.get(flag, False):
            printing = True

    return filename, printing, False   # we can't derive from octoprint_data if the job was cancelled. Always return true.

def settings_dict(octoprint_settings):
    return dict(('webcam_'+k, str(v)) for k, v in octoprint_settings['webcam'].items())

def process_octoprint_status_with_ts(op_status, printer):
    op_event = op_status.get('octoprint_event', {})
    op_data = op_status.get('octoprint_data', {})
    print_ts = op_status.get('current_print_ts')
    current_filename = op_event.get('name') or op_data.get('job', {}).get('file', {}).get('name')
    printer.update_current_print(current_filename, print_ts)
    if not printer.current_print:
        return

    if op_event.get('event_type') == 'PrintCancelled':
        printer.current_print.cancelled_at = timezone.now()
        printer.current_print.save()
    elif op_event.get('event_type') == 'PrintPaused':
        printer.current_print.paused_at = timezone.now()
        printer.current_print.save()
        PrintEvent.create(printer.current_print, PrintEvent.PAUSED)
    elif op_event.get('event_type') == 'PrintResumed':
        printer.current_print.paused_at = None
        printer.current_print.save()
        PrintEvent.create(printer.current_print, PrintEvent.RESUMED)
