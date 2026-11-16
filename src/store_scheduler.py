from __future__ import annotations
import queue, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from .database import now_iso
from .counter_manager import Counter
from .bo_controller import BOController

class StoreScheduler:
    def __init__(self, db, settings, logger, max_workers=100):
        self.db, self.settings, self.logger = db, settings, logger
        self.max_workers = max(1, min(100, int(max_workers)))
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.thread = None
        self.running = False
        self.active = set()

    def start(self):
        if self.running: return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def pause(self): self.pause_event.set()
    def resume(self): self.pause_event.clear()
    def stop(self):
        self.stop_event.set()
        self.pause_event.clear()

    def _loop(self):
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}
            while not self.stop_event.is_set() or futures:
                if not self.pause_event.is_set() and not self.stop_event.is_set():
                    now = datetime.now(timezone.utc)
                    for store in self.db.stores():
                        code = store['store_code']
                        if code in self.active: continue
                        if store['status'] == 'Pending':
                            self.active.add(code)
                            self.db.update_store(code, 'Processing')
                            future = pool.submit(self._process, store)
                            futures[future] = code
                            if len(futures) >= self.max_workers: break
                done = [f for f in futures if f.done()]
                for future in done:
                    code = futures.pop(future)
                    self.active.discard(code)
                    try: future.result()
                    except Exception as exc:
                        self.db.update_store(code, 'Failed', str(exc))
                        self.db.add_event(code, 'ERROR', str(exc))
                        self.events.put(('store', code))
                time.sleep(.4)
            self.running = False

    def _process(self, store):
        code = store['store_code']
        pending = self.db.counters(code, unfinished_only=True)
        if not pending:
            try:
                controller = BOController(store['bo_ip'], self.settings['bo_username'], self.settings['bo_password'],
                    int(self.settings['bo_port']), float(self.settings['bo_connect_timeout']), float(self.settings['bo_operation_timeout']), self.logger)
                controller.cleanup_job(store['job_id'])
            except Exception as exc:
                self.db.add_event(code, 'WARNING', f'All counters are successful, but BO staging cleanup failed: {exc}')
            self.db.update_store(code, 'Completed')
            self.events.put(('store', code)); return
        counters = [Counter(int(c['number']), c['ip']) for c in pending]
        cfg = self.settings
        controller = BOController(store['bo_ip'], cfg['bo_username'], cfg['bo_password'],
            int(cfg['bo_port']), float(cfg['bo_connect_timeout']), float(cfg['bo_operation_timeout']), self.logger)
        self.db.add_event(code, 'INFO', f"Starting attempt for {len(counters)} unfinished counter(s).")
        try:
            result = controller.run_distribution(
                local_file=store['update_path'], target_path=store['target_path'], counters=counters,
                counter_username=cfg['counter_username'], counter_password=cfg['counter_password'],
                counter_port=int(cfg['counter_port']), counter_connect_timeout=float(cfg['counter_connect_timeout']),
                counter_operation_timeout=float(cfg['counter_operation_timeout']), cancel_event=self.stop_event,
                transfer_mode=store['transfer_mode'], persistent_job_id=store['job_id'], cleanup_remote=False)
            returned = {str(r['counter']): r for r in result.results}
            for counter in counters:
                item = returned.get(str(counter.number))
                if item:
                    status = item['status']
                    self.db.update_counter(code, counter.number, status, item['message'], item.get('backup',''))
                else:
                    status = 'Failed'
                    self.db.update_counter(code, counter.number, status, 'No result returned by BO', '')
            unfinished = self.db.counters(code, unfinished_only=True)
            if not unfinished:
                controller.cleanup_job(store['job_id'])
                self.db.update_store(code, 'Completed')
                self.db.add_event(code, 'INFO', 'All counters completed; BO staging cleaned up.')
            else:
                error = '; '.join(f"{c['ip']}: {c['status']} {c['message']}" for c in unfinished[:8])
                self.db.update_store(code, 'Failed', error)
                self.db.add_event(code, 'WARNING', f'{len(unfinished)} counter(s) failed; no retries will be attempted.')
        except Exception as exc:
            for counter in pending:
                if counter['status'] == 'Pending':
                    self.db.update_counter(code, counter['number'], 'Failed', str(exc), '')
            self.db.update_store(code, 'Failed', str(exc))
            self.db.add_event(code, 'ERROR', str(exc))
        self.events.put(('store', code))
