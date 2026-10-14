"""Camera commands independent of station storage and network locks."""
import threading
import time
import uuid
import logging

log = logging.getLogger('camera.lifecycle')


class CameraLifecycle:
    def __init__(self, edge, notify, wait_seconds=35.0, notify_seconds=1.0):
        self.edge = edge
        self.notify = notify
        self.wait_seconds = wait_seconds
        self.notify_seconds = notify_seconds
        self.lock = threading.RLock()
        self.transition_lock = threading.Lock()
        self.operation = None
        self.revision = 0
        self.notification_pending = False
        self.record_operation = None
        self.closing = False

    def status(self):
        with self.lock:
            op = self.operation
            if (op is not None and op['done'].is_set()
                    and op['generation'] != getattr(self.edge, '_camera_generation', 0)):
                op = None
            if op is None:
                return {'phase': 'IDLE', 'pending': False}
            return {'operation_id': op['id'], 'phase': op['phase'],
                    'pending': not op['done'].is_set(),
                    'elapsed_seconds': max(0.0, (op.get('finished') or time.monotonic()) - op['started']),
                    'station_update': dict(op['station']), 'result': op.get('result')}

    def _notify(self, op):
        try:
            self.notify()
            result = {'success': True}
        except Exception as exc:
            result = {'success': False, 'error': str(exc)}
        with self.lock:
            op['station'] = result
            self.notification_pending = False
            if op.get('result') is not None:
                release = op['result']['camera_release']
                op['result'] = {**op['result'], 'station_update': result,
                                'success': bool(release.get('success')) and result['success'],
                                'error': '; '.join(x for x in (release.get('error'), result.get('error')) if x)}
        op['notified'].set()

    def _stop(self, op):
        try:
            release = self.edge.stop()
        except Exception as exc:
            release = {'success': False, 'safe': False, 'error': str(exc)}
        op['notified'].wait(self.notify_seconds)
        with self.lock:
            station = dict(op['station'])
            errors = [x for x in (release.get('error'), station.get('error')) if x]
            op['result'] = {**release, 'operation_id': op['id'],
                            'success': bool(release.get('success')) and station.get('success') is True,
                            'camera_release': release, 'station_update': station,
                            'error': '; '.join(errors)}
            op['phase'] = 'STOPPED' if release.get('safe', False) else 'RELEASE_FAILED'
            op['finished'] = time.monotonic()
            op['done'].set()
            log.info('STOP_FINISHED operation=%s phase=%s duration=%.3f safe=%s',
                     op['id'], op['phase'], op['finished'] - op['started'], release.get('safe'))

    def stop(self, explicit=True):
        with self.lock:
            if explicit:
                self.revision += 1
            op = self.operation
            if (op is not None and op['done'].is_set()
                    and op['generation'] != getattr(self.edge, '_camera_generation', 0)):
                op = None
            if op is None:
                op = {'id': uuid.uuid4().hex, 'started': time.monotonic(),
                      'phase': 'STOPPING', 'done': threading.Event(), 'notified': threading.Event(),
                      'generation': getattr(self.edge, '_camera_generation', 0),
                      'station': {'success': False, 'pending': True,
                                  'error': 'Station interruption update is pending'}}
                self.operation = op
                self.notification_pending = True
                # Stop acceptance cannot wait for station, scanner or database work.
                self.edge.stop_event.set()
                log.info('STOP_ACCEPTED operation=%s generation=%s', op['id'], op['generation'])
                threading.Thread(target=self._stop, args=(op,), daemon=True,
                                 name='camera-release').start()
                threading.Thread(target=self._notify, args=(op,), daemon=True,
                                 name='camera-station-notify').start()
        if not op['done'].wait(self.wait_seconds):
            return {'success': False, 'pending': True, 'operation_id': op['id'],
                    'error': 'Camera stop is still supervised; query lifecycle status'}
        return dict(op['result'])

    def _start(self):
        with self.lock:
            if self.closing:
                return {'success': False, 'error': 'Application is shutting down'}
            if self.notification_pending or (self.operation and not self.operation['done'].is_set()):
                return {'success': False, 'error': 'Camera or station cleanup is still pending'}
            if (self.operation and not self.operation['result'].get('safe', False)
                    and not self.operation['result'].get('reopen_eligible', False)):
                return {'success': False, 'error': 'Camera release is unconfirmed'}
            result = self.edge.start()
            if result.get('success'):
                self.operation = None
            return result

    def start(self):
        if not self.transition_lock.acquire(blocking=False):
            return {'success': False, 'error': 'Camera transition is in progress'}
        try:
            return self._start()
        finally:
            self.transition_lock.release()

    def configure(self, data=None, restart=False, validate=None):
        if not self.transition_lock.acquire(blocking=False):
            return {'success': False, 'error': 'Camera transition is in progress'}
        try:
            with self.lock:
                if self.closing:
                    return {'success': False, 'error': 'Application is shutting down'}
                revision = self.revision
            if validate is not None:
                rejected = validate()
                if rejected:
                    return {'success': False, 'error': rejected}
            if restart:
                result = self.stop(explicit=False)
                if not result.get('success'):
                    return result
            if data is not None:
                result = self.edge.update_config(data, restart=False)
                if not result.get('success'):
                    return result
            with self.lock:
                if revision != self.revision:
                    return {'success': False, 'error': 'Camera transition cancelled by Stop'}
                if restart:
                    return self._start()
                return result
        finally:
            self.transition_lock.release()

    def reset_recording(self):
        with self.lock:
            self.record_operation = None

    def stop_recording(self, wait_seconds=11.0):
        with self.lock:
            op = self.record_operation
            if op is None:
                op = {'done': threading.Event()}
                self.record_operation = op
                def finalize():
                    try:
                        op['result'] = self.edge.stop_recording()
                    except Exception as exc:
                        op['result'] = {'success': False, 'requires_camera_stop': True, 'error': str(exc)}
                    finally:
                        op['done'].set()
                threading.Thread(target=finalize, daemon=True, name='recording-finalize').start()
        if not op['done'].wait(wait_seconds):
            result = {'success': False, 'requires_camera_stop': True,
                      'error': 'Recording finalization timed out; stopping camera'}
        else:
            result = dict(op['result'])
        if result.get('requires_camera_stop'):
            result['camera_release'] = self.stop()
        return result
