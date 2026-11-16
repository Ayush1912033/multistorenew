from __future__ import annotations
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from .config_manager import ConfigManager
from .database import Database
from .excel_manager import read_stores
from .logger import create_logger
from .store_scheduler import StoreScheduler


class MultiStoreApp:
    def __init__(self, root: tk.Tk, base_dir: Path):
        self.root, self.base_dir = root, Path(base_dir)
        self.config_manager = ConfigManager(self.base_dir / 'config' / 'config.json')
        self.config = self.config_manager.load()
        self.db = Database(self.base_dir / 'data' / 'pos_distributor.db')
        self.logger = create_logger(self.base_dir / 'logs')
        self.scheduler = None
        self.selected_excel = tk.StringVar()
        self.update_mode = tk.StringVar(value='File')
        self.update_path = tk.StringVar()
        self.target_path = tk.StringVar()
        self.worker_count = tk.StringVar(value='100')
        self.status = tk.StringVar(value='Ready')
        self.bo_user = tk.StringVar(value=self.config['bo'].get('username',''))
        self.bo_password = tk.StringVar(value=self.config['bo'].get('password',''))
        self.bo_port = tk.StringVar(value=str(self.config['bo'].get('port',22)))
        self.bo_connect_timeout = tk.StringVar(value=str(self.config['bo'].get('connect_timeout',5)))
        self.bo_operation_timeout = tk.StringVar(value=str(self.config['bo'].get('operation_timeout',60)))
        self.counter_user = tk.StringVar(value=self.config['counter'].get('username',''))
        self.counter_password = tk.StringVar(value=self.config['counter'].get('password',''))
        self.counter_port = tk.StringVar(value=str(self.config['counter'].get('port',22)))
        self.counter_connect_timeout = tk.StringVar(value=str(self.config['counter'].get('connect_timeout',5)))
        self.counter_operation_timeout = tk.StringVar(value=str(self.config['counter'].get('operation_timeout',60)))
        self.root.title('POS File Distributor — Multi-Store Scheduler')
        self.root.geometry('1180x760')
        self.root.minsize(900, 600)
        self._build()
        self.refresh()
        self.root.after(400, self._poll_events)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def _build(self):
        outer = ttk.Frame(self.root, padding=12); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='POS File Distributor', font=('Segoe UI',20,'bold')).pack(anchor='w')
        ttk.Label(outer, text='Multi-store processing • No automatic retries • Retry failed stores manually').pack(anchor='w', pady=(0,10))
        credentials = ttk.LabelFrame(outer, text='Connection settings (shared across stores)', padding=8); credentials.pack(fill='x', pady=(0,8))
        fields = [
            ('BO username', self.bo_user, False), ('BO password', self.bo_password, True), ('BO SSH port', self.bo_port, False),
            ('BO connect timeout', self.bo_connect_timeout, False), ('BO operation timeout', self.bo_operation_timeout, False),
            ('Counter username', self.counter_user, False), ('Counter password', self.counter_password, True), ('Counter SSH port', self.counter_port, False),
            ('Counter connect timeout', self.counter_connect_timeout, False), ('Counter operation timeout', self.counter_operation_timeout, False),
        ]
        for i,(label,var,secret) in enumerate(fields):
            r,c=divmod(i,5)
            ttk.Label(credentials,text=label).grid(row=r*2,column=c,sticky='w',padx=4,pady=(2,0))
            ttk.Entry(credentials,textvariable=var,show='*' if secret else '',width=20).grid(row=r*2+1,column=c,sticky='ew',padx=4,pady=(0,4))
            credentials.columnconfigure(c,weight=1)
        source = ttk.LabelFrame(outer, text='Job input', padding=8); source.pack(fill='x', pady=(0,8))
        ttk.Label(source,text='Stores Excel (.xlsx)').grid(row=0,column=0,sticky='w',padx=4,pady=4)
        ttk.Entry(source,textvariable=self.selected_excel).grid(row=0,column=1,sticky='ew',padx=4,pady=4)
        ttk.Button(source,text='Browse Excel',command=self.browse_excel).grid(row=0,column=2,padx=4,pady=4)
        ttk.Label(source,text='Update type').grid(row=1,column=0,sticky='w',padx=4,pady=4)
        mode=ttk.Combobox(source,textvariable=self.update_mode,values=('File','Folder Update'),state='readonly',width=18); mode.grid(row=1,column=1,sticky='w',padx=4,pady=4); mode.bind('<<ComboboxSelected>>',self._mode_change)
        ttk.Label(source,text='Update file/folder').grid(row=2,column=0,sticky='w',padx=4,pady=4)
        ttk.Entry(source,textvariable=self.update_path).grid(row=2,column=1,sticky='ew',padx=4,pady=4)
        ttk.Button(source,text='Browse update',command=self.browse_update).grid(row=2,column=2,padx=4,pady=4)
        self.target_path_label = ttk.Label(source,text='Target file path on counters')
        self.target_path_label.grid(row=3,column=0,sticky='w',padx=4,pady=4)
        ttk.Entry(source,textvariable=self.target_path).grid(row=3,column=1,sticky='ew',padx=4,pady=4)
        ttk.Label(source,text='Concurrent stores (1–100)').grid(row=4,column=0,sticky='w',padx=4,pady=4)
        ttk.Spinbox(source,from_=1,to=100,textvariable=self.worker_count,width=8).grid(row=4,column=1,sticky='w',padx=4,pady=4)
        source.columnconfigure(1,weight=1)
        actions=ttk.Frame(outer); actions.pack(fill='x',pady=(0,8))
        ttk.Button(actions,text='Start / Load Excel',command=self.start_job).pack(side='left',padx=(0,5))
        ttk.Button(actions,text='Resume Saved Job',command=self.resume_saved_job).pack(side='left',padx=5)
        ttk.Button(actions,text='Retry Failed',command=self.retry_failed).pack(side='left',padx=5)
        ttk.Button(actions,text='Pause',command=self.pause).pack(side='left',padx=5)
        ttk.Button(actions,text='Resume',command=self.resume).pack(side='left',padx=5)
        ttk.Button(actions,text='Refresh',command=self.refresh).pack(side='left',padx=5)
        ttk.Button(actions,text='Reset Current Job',command=self.reset).pack(side='left',padx=5)
        ttk.Label(actions,textvariable=self.status,font=('Segoe UI',10,'bold')).pack(side='right')
        paned=ttk.Panedwindow(outer,orient='vertical'); paned.pack(fill='both',expand=True)
        stores_frame=ttk.LabelFrame(paned,text='Stores / queues',padding=5); counters_frame=ttk.LabelFrame(paned,text='Selected store — counter-level status',padding=5)
        paned.add(stores_frame,weight=3); paned.add(counters_frame,weight=2)
        cols=('store','bo','count','status','error')
        self.store_tree=ttk.Treeview(stores_frame,columns=cols,show='headings',selectmode='browse')
        for col,label,width in [('store','Store Code',100),('bo','BO IP',125),('count','Counters',70),('status','Queue / Status',120),('error','Last Error / Summary',580)]:
            self.store_tree.heading(col,text=label); self.store_tree.column(col,width=width,anchor='w')
        sy=ttk.Scrollbar(stores_frame,orient='vertical',command=self.store_tree.yview)
        sx=ttk.Scrollbar(stores_frame,orient='horizontal',command=self.store_tree.xview)
        self.store_tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        stores_frame.columnconfigure(0,weight=1); stores_frame.rowconfigure(0,weight=1)
        self.store_tree.grid(row=0,column=0,sticky='nsew')
        sy.grid(row=0,column=1,sticky='ns')
        sx.grid(row=1,column=0,sticky='ew')
        self.store_tree.bind('<<TreeviewSelect>>',self.show_counters)
        ccols=('number','ip','status','attempts','message','backup')
        self.counter_tree=ttk.Treeview(counters_frame,columns=ccols,show='headings')
        for col,label,width in [('number','Counter',65),('ip','IP',125),('status','Status',105),('attempts','Attempts',70),('message','Details',380),('backup','Backup',240)]:
            self.counter_tree.heading(col,text=label); self.counter_tree.column(col,width=width,anchor='w')
        csy=ttk.Scrollbar(counters_frame,orient='vertical',command=self.counter_tree.yview)
        csx=ttk.Scrollbar(counters_frame,orient='horizontal',command=self.counter_tree.xview)
        self.counter_tree.configure(yscrollcommand=csy.set,xscrollcommand=csx.set)
        counters_frame.columnconfigure(0,weight=1); counters_frame.rowconfigure(0,weight=1)
        self.counter_tree.grid(row=0,column=0,sticky='nsew')
        csy.grid(row=0,column=1,sticky='ns')
        csx.grid(row=1,column=0,sticky='ew')
        ttk.Label(outer,text='Excel columns: StoreCode | BO_IP | CounterCount | CounterIPs (optional). Custom IPs: comma/semicolon separated; missing IPs require confirmation.').pack(anchor='w',pady=(6,0))

    def _mode_change(self,_event=None):
        label = 'Target parent folder on counters' if self.update_mode.get() == 'Folder Update' else 'Target file path on counters'
        self.target_path_label.configure(text=label)
    def browse_excel(self):
        p=filedialog.askopenfilename(title='Select store list',filetypes=[('Excel workbook','*.xlsx')])
        if p:self.selected_excel.set(p)
    def browse_update(self):
        if self.update_mode.get()=='Folder Update': p=filedialog.askdirectory(title='Select update folder')
        else:p=filedialog.askopenfilename(title='Select replacement file')
        if p:self.update_path.set(p)

    def _settings(self):
        return {'bo_username':self.bo_user.get().strip(),'bo_password':self.bo_password.get(),'bo_port':self.bo_port.get().strip(),
            'bo_connect_timeout':self.bo_connect_timeout.get().strip(),'bo_operation_timeout':self.bo_operation_timeout.get().strip(),
            'counter_username':self.counter_user.get().strip(),'counter_password':self.counter_password.get(),'counter_port':self.counter_port.get().strip(),
            'counter_connect_timeout':self.counter_connect_timeout.get().strip(),'counter_operation_timeout':self.counter_operation_timeout.get().strip()}

    def _validate(self):
        s=self._settings()
        for key in ('bo_username','bo_password','counter_username','counter_password'):
            if not s[key]: raise ValueError(f'{key.replace("_"," ").title()} is required.')
        for key in ('bo_port','counter_port'):
            n=int(s[key]);
            if not 1<=n<=65535: raise ValueError(f'{key} must be 1–65535.')
        for key in ('bo_connect_timeout','bo_operation_timeout','counter_connect_timeout','counter_operation_timeout'):
            if float(s[key])<=0: raise ValueError(f'{key} must be greater than zero.')
        workers=int(self.worker_count.get())
        if not 1<=workers<=100: raise ValueError('Concurrent stores must be 1–100.')
        if not self.selected_excel.get() or not Path(self.selected_excel.get()).is_file(): raise ValueError('Select a valid Excel file.')
        update=Path(self.update_path.get())
        if self.update_mode.get()=='Folder Update':
            if not update.is_dir(): raise ValueError('Select a valid update folder.')
        elif not update.is_file(): raise ValueError('Select a valid update file.')
        if not self.target_path.get().strip().startswith('/'): raise ValueError('Target path must be an absolute Linux path.')
        return s,workers

    def start_job(self):
        try:
            settings,workers=self._validate()
            rows=read_stores(self.selected_excel.get())
        except Exception as exc:
            messagebox.showerror('Validation',str(exc)); return

        partial_custom = [
            row for row in rows
            if row.get('CounterIPs') and len(row['CounterIPs']) < int(row['CounterCount'])
        ]
        if partial_custom:
            details = "\\n".join(
                f"{row['StoreCode']}: {len(row['CounterIPs'])} custom IP(s), "
                f"{int(row['CounterCount']) - len(row['CounterIPs'])} to generate"
                for row in partial_custom[:12]
            )
            if len(partial_custom) > 12:
                details += f"\\n...and {len(partial_custom) - 12} more store(s)"
            proceed = messagebox.askyesno(
                'Generate missing counter IPs?',
                "Some stores have fewer custom CounterIPs than CounterCount.\\n\\n"
                + details
                + "\\n\\nIf you continue, the missing IPs will be generated automatically "
                  "from each BO subnet starting at .112, skipping duplicate IPs. "
                  "This may target IPs that do not exist in your test network.\\n\\n"
                  "Continue?"
            )
            if not proceed:
                self.status.set('Excel load cancelled — no job changes made')
                return

        if self.scheduler and self.scheduler.running:
            messagebox.showwarning('Already running','Pause or stop the current job before loading a new Excel file.'); return
        if self.db.stores() and not messagebox.askyesno('Replace current job', 'Loading Excel replaces the current job and its queue data. Continue?'):
            return
        self.config['bo'].update({'username':settings['bo_username'],'password':settings['bo_password'],'port':int(settings['bo_port']),'connect_timeout':float(settings['bo_connect_timeout']),'operation_timeout':float(settings['bo_operation_timeout'])})
        self.config['counter'].update({'username':settings['counter_username'],'password':settings['counter_password'],'port':int(settings['counter_port']),'connect_timeout':float(settings['counter_connect_timeout']),'operation_timeout':float(settings['counter_operation_timeout'])})
        self.config_manager.save(self.config)
        self.db.replace_job(rows,self.target_path.get().strip(),self.update_path.get().strip(),'folder' if self.update_mode.get()=='Folder Update' else 'file')
        self.scheduler=StoreScheduler(self.db,settings,self.logger,workers)
        self.scheduler.start(); self.status.set(f'Running — {len(rows)} stores / {workers} workers'); self.refresh()

    def resume_saved_job(self):
        stores = self.db.stores()
        if not stores:
            messagebox.showinfo('No saved job', 'There is no saved store job to resume. Upload an Excel workbook first.')
            return
        if self.scheduler and self.scheduler.running:
            messagebox.showinfo('Already running', 'The scheduler is already running.')
            return
        try:
            settings = self._settings()
            for key in ('bo_username','bo_password','counter_username','counter_password'):
                if not settings[key]: raise ValueError(f'{key.replace("_"," ").title()} is required.')
            workers = int(self.worker_count.get())
            if not 1 <= workers <= 100: raise ValueError('Concurrent stores must be 1–100.')
            # Paths and update type are restored from SQLite; credentials remain in config.json / UI.
            self.scheduler = StoreScheduler(self.db, settings, self.logger, workers)
            self.scheduler.start()
            self.status.set(f'Resuming saved job — {len(stores)} stores / {workers} workers')
            self.refresh()
        except Exception as exc:
            messagebox.showerror('Resume saved job', str(exc))

    def retry_failed(self):
        if self.scheduler and self.scheduler.running and self.scheduler.active:
            messagebox.showwarning('Attempts still active', 'Wait for active store attempts to finish before retrying failed stores.')
            return
        try:
            settings = self._settings()
            for key in ('bo_username','bo_password','counter_username','counter_password'):
                if not settings[key]: raise ValueError(f'{key.replace("_"," ").title()} is required.')
            workers = int(self.worker_count.get())
            if not 1 <= workers <= 100: raise ValueError('Concurrent stores must be 1–100.')
            count = self.db.retry_failed()
            if not count:
                messagebox.showinfo('No failed stores', 'There are no failed stores to retry.')
                return
            if self.scheduler and self.scheduler.running:
                self.scheduler.settings = settings
                self.scheduler.resume()
            else:
                self.scheduler = StoreScheduler(self.db, settings, self.logger, workers)
                self.scheduler.start()
            self.status.set(f'Manually retrying {count} failed store(s)')
            self.refresh()
        except Exception as exc:
            messagebox.showerror('Retry failed stores', str(exc))

    def pause(self):
        if self.scheduler:self.scheduler.pause(); self.status.set('Paused — active store attempts will finish')
    def resume(self):
        if self.scheduler:self.scheduler.resume(); self.status.set('Running')

    def refresh(self):
        selected=self.store_tree.selection()[0] if self.store_tree.selection() else None
        for item in self.store_tree.get_children():self.store_tree.delete(item)
        stores=self.db.stores()
        for s in stores:
            error=s.get('last_error','') or ''
            self.store_tree.insert('','end',iid=s['store_code'],values=(s['store_code'],s['bo_ip'],s['counter_count'],s['status'],error[:580]))
        if selected and self.store_tree.exists(selected):self.store_tree.selection_set(selected); self.show_counters()
        summary=self.db.summary()
        self.status.set(' | '.join(f'{k}: {v}' for k,v in sorted(summary.items())) if summary else 'Ready')

    def show_counters(self,_event=None):
        selection=self.store_tree.selection()
        if not selection:return
        code=selection[0]
        for item in self.counter_tree.get_children():self.counter_tree.delete(item)
        for c in self.db.counters(code):
            self.counter_tree.insert('','end',values=(c['number'],c['ip'],c['status'],c['attempts'],c['message'],c['backup']))

    def _poll_events(self):
        if self.scheduler:
            try:
                while True:self.scheduler.events.get_nowait(); self.refresh()
            except Exception:pass
        self.root.after(500,self._poll_events)

    def reset(self):
        if self.scheduler and self.scheduler.running:
            messagebox.showwarning('Scheduler running','Pause the scheduler and wait for active attempts to finish before resetting.'); return
        if not messagebox.askyesno('Confirm reset','This clears the current store queues, counter progress, and event history. You will need to upload Excel again. Continue?'):return
        self.db.reset(); self.selected_excel.set(''); self.refresh(); self.status.set('Reset complete — upload Excel to start a new job')

    def close(self):
        if self.scheduler and self.scheduler.running:
            if not messagebox.askyesno('Close application','The scheduler will stop dispatching new stores. Active SSH operations may finish in the background. Saved progress will be restored on next launch. Close anyway?'):return
            self.scheduler.stop()
        self.root.destroy()
