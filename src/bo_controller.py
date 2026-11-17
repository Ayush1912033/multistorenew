from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

import paramiko


UNREACHABLE = (
    socket.timeout,
    TimeoutError,
    ConnectionError,
    paramiko.ssh_exception.NoValidConnectionsError,
)


@dataclass
class JobResult:
    status: str
    message: str
    results: list[dict]


class BOController:
    def __init__(
        self,
        bo_ip,
        bo_username,
        bo_password,
        bo_port=22,
        connect_timeout=5,
        operation_timeout=120,
        logger=None,
    ):
        self.bo_ip = bo_ip
        self.bo_username = bo_username
        self.bo_password = bo_password
        self.bo_port = int(bo_port)
        self.connect_timeout = float(connect_timeout)
        self.operation_timeout = float(operation_timeout)
        self.logger = logger

    def _log(self, message):
        if self.logger:
            self.logger.info(message)

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(
            hostname=self.bo_ip,
            port=self.bo_port,
            username=self.bo_username,
            password=self.bo_password,
            timeout=self.connect_timeout,
            banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )

        transport = client.get_transport()
        if transport:
            transport.set_keepalive(10)

        sftp = client.open_sftp()
        sftp.get_channel().settimeout(self.operation_timeout)
        return client, sftp

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def test_bo(self):
        client = None
        sftp = None
        try:
            client, sftp = self._connect()
            sftp.stat(".")
            return True, "BO SSH/SFTP connection successful."
        except UNREACHABLE:
            return False, "BO could not be reached."
        except Exception as exc:
            return False, str(exc)
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass
            if client:
                try:
                    client.close()
                except Exception:
                    pass


    def run_distribution(
        self,
        local_file,
        target_path,
        counters,
        counter_username,
        counter_password,
        counter_port,
        counter_connect_timeout,
        counter_operation_timeout,
        cancel_event=None,
        transfer_mode="file",
        persistent_job_id=None,
        cleanup_remote=True,
    ):
        """Distribute a single file or recursively merge a folder update."""
        local_path = Path(local_file)
        transfer_mode = str(transfer_mode or "file").strip().lower()

        if transfer_mode not in ("file", "folder", "zip"):
            raise ValueError("Transfer mode must be 'file', 'folder', or 'zip'.")

        if transfer_mode in ("file", "zip"):
            if not local_path.is_file():
                raise ValueError("Replacement file does not exist.")
            if transfer_mode == "zip" and local_path.suffix.lower() != ".zip":
                raise ValueError("ZIP update must be a .zip archive.")
        else:
            if not local_path.is_dir():
                raise ValueError("Update folder does not exist.")

        if not target_path.startswith("/"):
            raise ValueError("Counter parent path must be an absolute Linux path.")
        if transfer_mode == "folder":
            folder_name = local_path.name
            if folder_name in ("", ".", ".."):
                raise ValueError("Update folder must have a valid name.")
            target_path = posixpath.join(target_path.rstrip("/") or "/", folder_name)
        if not counters:
            raise ValueError("At least one counter is required.")

        job_id = str(persistent_job_id or uuid.uuid4().hex[:12])
        if not job_id or any(ch not in "0123456789abcdefABCDEF-_" for ch in job_id):
            raise ValueError("Invalid persistent job ID.")
        remote_root = f"/tmp/pos_file_distributor/{job_id}"
        remote_file = f"{remote_root}/{local_path.name}"
        remote_update = f"{remote_root}/update"
        remote_manifest = f"{remote_root}/manifest.txt"
        remote_counter_script = f"{remote_root}/counter_folder_apply.sh"
        remote_package = f"{remote_root}/counter_update.tar.gz"
        remote_script = f"{remote_root}/distribute.sh"
        remote_password = f"{remote_root}/.counter_password"
        remote_config = f"{remote_root}/counters.json"

        local_hash = self._sha256(local_path) if transfer_mode in ("file", "zip") else ""
        manifest_count = 0
        client = None
        sftp = None

        try:
            client, sftp = self._connect()
            output, error, exit_code = self._exec(
                client,
                "mkdir -p /tmp/pos_file_distributor && "
                f"mkdir -p {shlex.quote(remote_root)} && "
                f"chmod 700 {shlex.quote(remote_root)}",
            )
            if exit_code != 0:
                raise RuntimeError(
                    "Could not create BO temporary directory: "
                    f"{error.strip() or output.strip()}"
                )

            if transfer_mode in ("file", "zip"):
                sftp.put(str(local_path), remote_file)
                sftp.chmod(remote_file, 0o600)
            else:
                output, error, exit_code = self._exec(
                    client,
                    f"mkdir -p {shlex.quote(remote_update)} && chmod 700 {shlex.quote(remote_update)}",
                )
                if exit_code != 0:
                    raise RuntimeError(
                        "Could not create BO update directory: "
                        f"{error.strip() or output.strip()}"
                    )
                manifest_count = self._upload_folder(
                    sftp, local_path, remote_update, remote_manifest
                )

            password_bytes = (counter_password + "\n").encode("utf-8")
            with sftp.open(remote_password, "wb") as f:
                f.write(password_bytes)
            sftp.chmod(remote_password, 0o600)

            counter_data = [
                {"number": c.number, "ip": c.ip, "source": c.source}
                for c in counters
            ]
            with sftp.open(remote_config, "w") as f:
                json.dump(counter_data, f)
            sftp.chmod(remote_config, 0o600)

            if transfer_mode == "folder":
                counter_script = self._build_counter_folder_script()
                with sftp.open(remote_counter_script, "w") as f:
                    f.write(counter_script)
                sftp.chmod(remote_counter_script, 0o700)
                package_cmd = (
                    f"tar -czf {shlex.quote(remote_package)} "
                    f"-C {shlex.quote(remote_root)} update manifest.txt counter_folder_apply.sh"
                )
                _output, package_error, package_exit = self._exec(
                    client,
                    package_cmd,
                    timeout=self.operation_timeout * max(1, len(counters)),
                )
                if package_exit != 0:
                    raise RuntimeError(
                        "Could not package folder update on BO: "
                        f"{package_error.strip() or 'tar failed'}"
                    )

            script = self._build_script(
                remote_file=remote_file,
                remote_update=remote_update,
                remote_manifest=remote_manifest,
                remote_counter_script=remote_counter_script,
                remote_package=remote_package,
                target_path=target_path,
                remote_password=remote_password,
                remote_config=remote_config,
                counter_username=counter_username,
                counter_port=int(counter_port),
                connect_timeout=int(counter_connect_timeout),
                operation_timeout=int(counter_operation_timeout),
                expected_hash=local_hash,
                manifest_count=manifest_count,
                job_id=job_id,
                transfer_mode=transfer_mode,
                counter_lines=[(str(c.number), str(c.ip), str(c.source)) for c in counters],
            )

            with sftp.open(remote_script, "w") as f:
                f.write(script)
            sftp.chmod(remote_script, 0o700)

            if cancel_event and cancel_event.is_set():
                return JobResult("Cancelled", "Cancelled before BO execution.", [])

            self._log(
                f"Starting BO distribution: {len(counters)} counter(s), mode={transfer_mode}, target={target_path}"
            )
            if transfer_mode == "folder":
                self._log(f"Folder update contains {manifest_count} file(s).")

            output, error, exit_code = self._exec(
                client,
                f"bash {shlex.quote(remote_script)}",
                timeout=self.operation_timeout * max(1, len(counters)),
            )
            if output.strip():
                self._log(f"BO distribution output:\n{output.strip()}")
            if error.strip():
                self._log(f"BO distribution stderr:\n{error.strip()}")

            results = self._parse_results(output)
            if exit_code != 0 and not results:
                return JobResult(
                    "Failed",
                    f"BO distribution script failed (exit code {exit_code}): {error.strip() or output.strip() or 'No output'}",
                    [],
                )
            if not results:
                return JobResult(
                    "Failed",
                    "BO distribution completed but returned no counter results. Check the BO distribution log.",
                    [],
                )
            return JobResult("Complete", "BO distribution finished.", results)

        except Exception as exc:
            self._log(f"Distribution error: {exc}")
            raise
        finally:
            if client and cleanup_remote:
                try:
                    self._exec(client, f"rm -rf {shlex.quote(remote_root)}", timeout=20)
                    self._exec(
                        client,
                        "find /tmp/pos_file_distributor -mindepth 1 -maxdepth 1 -type d -empty -delete",
                        timeout=20,
                    )
                except Exception as exc:
                    self._log(f"BO cleanup warning: {exc}")
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def cleanup_job(self, job_id):
        """Remove retained BO staging after every required counter has succeeded."""
        job_id = str(job_id)
        if not job_id or any(ch not in "0123456789abcdefABCDEF-_" for ch in job_id):
            raise ValueError("Invalid persistent job ID.")
        client = None
        try:
            client, _sftp = self._connect()
            root = f"/tmp/pos_file_distributor/{job_id}"
            _out, error, code = self._exec(client, f"rm -rf {shlex.quote(root)}", timeout=20)
            if code != 0:
                raise RuntimeError(error.strip() or "Could not clean up BO staging directory.")
            self._exec(client, "find /tmp/pos_file_distributor -mindepth 1 -maxdepth 1 -type d -empty -delete", timeout=20)
            self._log(f"Cleaned BO staging for job {job_id}.")
        finally:
            if client:
                client.close()

    @staticmethod
    def _upload_folder(sftp, local_folder, remote_folder, remote_manifest):
        """Upload a complete update tree and create a SHA-256 manifest."""
        local_folder = Path(local_folder)
        directories = []
        files = []

        for root, dir_names, file_names in os.walk(local_folder, topdown=True, followlinks=False):
            root_path = Path(root)
            for name in dir_names:
                path = root_path / name
                if path.is_symlink():
                    raise ValueError(f"Folder update contains an unsupported symbolic link: {path}")
                directories.append(path)
            for name in file_names:
                path = root_path / name
                if path.is_symlink():
                    raise ValueError(f"Folder update contains an unsupported symbolic link: {path}")
                files.append(path)

        for directory in sorted(directories, key=lambda p: len(p.parts)):
            relative_text = directory.relative_to(local_folder).as_posix()
            BOController._validate_manifest_path(relative_text)
            remote_path = f"{remote_folder}/{relative_text}"
            try:
                sftp.mkdir(remote_path, mode=0o700)
            except OSError:
                try:
                    sftp.stat(remote_path)
                except OSError as exc:
                    raise RuntimeError(f"Could not create BO update directory: {remote_path}") from exc

        manifest_lines = []
        for local_path in files:
            relative_text = local_path.relative_to(local_folder).as_posix()
            BOController._validate_manifest_path(relative_text)
            remote_path = f"{remote_folder}/{relative_text}"
            sftp.put(str(local_path), remote_path)
            sftp.chmod(remote_path, 0o600)
            manifest_lines.append(f"{BOController._sha256(local_path)}|{relative_text}\n")

        with sftp.open(remote_manifest, "wb") as f:
            f.write("".join(manifest_lines).encode("utf-8"))
        sftp.chmod(remote_manifest, 0o600)
        return len(files)

    @staticmethod
    def _validate_manifest_path(relative_path):
        if not relative_path:
            return
        if any(ch in relative_path for ch in ("|", "\t", "\r", "\n")):
            raise ValueError("Folder update contains a path with an unsupported delimiter character.")
        if relative_path.startswith(("/", "\\")):
            raise ValueError(f"Folder update contains an invalid absolute path: {relative_path}")
        parts = relative_path.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"Folder update contains an invalid relative path: {relative_path}")
    def _exec(self, client, command, timeout=None):
        stdin, stdout, stderr = client.exec_command(
            command,
            timeout=timeout or self.operation_timeout,
        )

        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")

        return output, error, exit_code

    @staticmethod
    def _build_file_script(
        remote_file,
        target_path,
        remote_password,
        remote_config,
        counter_username,
        counter_port,
        connect_timeout,
        operation_timeout,
        expected_hash,
        counter_lines,
    ):
        """
        Build the shell script executed on the BO.

        Important diagnostic changes compared with the old version:
        - SCP/SSH errors are captured instead of discarded.
        - Exact command errors are returned in RESULT messages.
        - Each counter runs independently in the background.
        - The parent shell waits for every counter job.
        - Temporary upload is SHA-256 verified before replacement.
        - Final target is SHA-256 verified after replacement.
        """

        # Embed the already-validated counter list directly into the script.
        # This removes the BO-side python3/JSON parsing dependency.
        # Use a delimiter that cannot occur in an IPv4 address/counter number.
        # Do NOT embed literal \t sequences in single-quoted Bash strings:
        # Bash would keep them as backslash+t instead of a real tab.
        counter_records = [
            f"{number}|{ip}|{source}"
            for number, ip, source in counter_lines
        ]
        counter_block = "\n".join(
            "    " + shlex.quote(record)
            for record in counter_records
        )

        return f"""#!/usr/bin/env bash
set -u

REMOTE_FILE={shlex.quote(remote_file)}
TARGET={shlex.quote(target_path)}
PASSWORD_FILE={shlex.quote(remote_password)}
COUNTERS_FILE={shlex.quote(remote_config)}
COUNTER_USER={shlex.quote(counter_username)}
PORT={counter_port}
CONNECT_TIMEOUT={connect_timeout}
OP_TIMEOUT={operation_timeout}
EXPECTED_HASH={shlex.quote(expected_hash)}

emit() {{
    printf 'RESULT\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$1" "$2" "$3" "$4" "$5"
}}

if ! command -v sshpass >/dev/null 2>&1; then
    emit "BO" "0.0.0.0" "Failed" \
        "sshpass is not installed on BO" ""
    exit 2
fi

if ! command -v scp >/dev/null 2>&1; then
    emit "BO" "0.0.0.0" "Failed" \
        "scp is not installed on BO" ""
    exit 2
fi

if ! command -v ssh >/dev/null 2>&1; then
    emit "BO" "0.0.0.0" "Failed" \
        "ssh is not installed on BO" ""
    exit 2
fi

if ! command -v timeout >/dev/null 2>&1; then
    emit "BO" "0.0.0.0" "Failed" \
        "timeout command is not installed on BO" ""
    exit 2
fi

if ! command -v sha256sum >/dev/null 2>&1; then
    emit "BO" "0.0.0.0" "Failed" \
        "sha256sum is not installed on BO" ""
    exit 2
fi

distribute_one() {{
    NUMBER="$1"
    IP="$2"

    # ------------------------------------------------------------
    # 1. Connectivity/authentication test
    # ------------------------------------------------------------
    SSH_CHECK_OUTPUT=$(
        timeout "$CONNECT_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o BatchMode=no \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" "true" 2>&1
    )
    SSH_CHECK_EXIT=$?

    if [ "$SSH_CHECK_EXIT" -ne 0 ]; then
        # Keep the UI readable while still exposing the actual error.
        SSH_CHECK_OUTPUT=$(printf '%s' "$SSH_CHECK_OUTPUT" | tr '\\n' ' ' | cut -c1-300)

        if [ -z "$SSH_CHECK_OUTPUT" ]; then
            SSH_CHECK_OUTPUT="SSH connection failed (exit code $SSH_CHECK_EXIT)"
        fi

        emit "$NUMBER" "$IP" "Device Off" \
            "$SSH_CHECK_OUTPUT" ""
        return 0
    fi

    REMOTE_DIR=$(dirname "$TARGET")
    REMOTE_TMP="${{TARGET}}.posdist_tmp_$$"
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP="${{TARGET}}.bak_${{TIMESTAMP}}"

    # ------------------------------------------------------------
    # 2. Verify target directory
    # ------------------------------------------------------------
    DIR_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" \\
            "test -d $(printf '%q' "$REMOTE_DIR")" 2>&1
    )
    DIR_EXIT=$?

    if [ "$DIR_EXIT" -ne 0 ]; then
        DIR_OUTPUT=$(printf '%s' "$DIR_OUTPUT" | tr '\\n' ' ' | cut -c1-300)

        if [ -z "$DIR_OUTPUT" ]; then
            DIR_OUTPUT="Target directory does not exist or is not accessible"
        fi

        emit "$NUMBER" "$IP" "Failed" \
            "$DIR_OUTPUT" ""
        return 0
    fi

    # ------------------------------------------------------------
    # 3. Check whether target directory is writable
    # ------------------------------------------------------------
    WRITE_TEST=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" \\
            "touch $(printf '%q' "${{REMOTE_DIR}}/.posdist_write_test_$$") && rm -f $(printf '%q' "${{REMOTE_DIR}}/.posdist_write_test_$$")" \\
            2>&1
    )
    WRITE_EXIT=$?

    if [ "$WRITE_EXIT" -ne 0 ]; then
        WRITE_TEST=$(printf '%s' "$WRITE_TEST" | tr '\\n' ' ' | cut -c1-300)

        if [ -z "$WRITE_TEST" ]; then
            WRITE_TEST="Target directory is not writable"
        fi

        emit "$NUMBER" "$IP" "Failed" \
            "Target directory not writable: $WRITE_TEST" ""
        return 0
    fi

    # ------------------------------------------------------------
    # 4. Capture existing target permissions
    # ------------------------------------------------------------
    OLD_MODE=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" \\
            "if [ -f $(printf '%q' "$TARGET") ]; then stat -c '%a' $(printf '%q' "$TARGET"); fi" \\
            2>/dev/null || true
    )

    # ------------------------------------------------------------
    # 5. Upload file to temporary path
    # ------------------------------------------------------------
    SCP_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" scp \\
            -q \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -P "$PORT" \\
            "$REMOTE_FILE" \\
            "$COUNTER_USER@$IP:$REMOTE_TMP" 2>&1
    )
    SCP_EXIT=$?

    if [ "$SCP_EXIT" -ne 0 ]; then
        SCP_OUTPUT=$(printf '%s' "$SCP_OUTPUT" | tr '\\n' ' ' | cut -c1-400)

        if [ -z "$SCP_OUTPUT" ]; then
            SCP_OUTPUT="SCP upload failed (exit code $SCP_EXIT)"
        fi

        emit "$NUMBER" "$IP" "Failed" \
            "SCP upload failed: $SCP_OUTPUT" ""
        return 0
    fi

    # ------------------------------------------------------------
    # 6. Verify temporary file SHA-256
    # ------------------------------------------------------------
    HASH_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" \\
            "sha256sum $(printf '%q' "$REMOTE_TMP")" 2>&1
    )
    HASH_EXIT=$?

    if [ "$HASH_EXIT" -ne 0 ]; then
        HASH_OUTPUT=$(printf '%s' "$HASH_OUTPUT" | tr '\\n' ' ' | cut -c1-400)

        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o LogLevel=ERROR \\
            -p "$PORT" "$COUNTER_USER@$IP" \\
            "rm -f $(printf '%q' "$REMOTE_TMP")" >/dev/null 2>&1 || true

        emit "$NUMBER" "$IP" "Failed" \
            "Could not calculate remote SHA-256: $HASH_OUTPUT" ""
        return 0
    fi

    REMOTE_HASH=$(printf '%s\\n' "$HASH_OUTPUT" | awk '{{print $1}}')

    if [ "$REMOTE_HASH" != "$EXPECTED_HASH" ]; then
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o LogLevel=ERROR \\
            -p "$PORT" "$COUNTER_USER@$IP" \\
            "rm -f $(printf '%q' "$REMOTE_TMP")" >/dev/null 2>&1 || true

        emit "$NUMBER" "$IP" "Failed" \\
            "SHA-256 verification failed (expected $EXPECTED_HASH, got $REMOTE_HASH)" ""
        return 0
    fi

    # ------------------------------------------------------------
    # 7. Backup old target and replace it
    # ------------------------------------------------------------
    REMOTE_CMD="if [ -f $(printf '%q' "$TARGET") ]; then cp -p $(printf '%q' "$TARGET") $(printf '%q' "$BACKUP") || exit 20; fi; mv $(printf '%q' "$REMOTE_TMP") $(printf '%q' "$TARGET") || exit 21"

    if [ -n "$OLD_MODE" ]; then
        REMOTE_CMD="$REMOTE_CMD; chmod $OLD_MODE $(printf '%q' "$TARGET") || exit 22"
    fi

    REPLACE_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" "$REMOTE_CMD" 2>&1
    )
    REPLACE_EXIT=$?

    if [ "$REPLACE_EXIT" -ne 0 ]; then
        REPLACE_OUTPUT=$(printf '%s' "$REPLACE_OUTPUT" | tr '\\n' ' ' | cut -c1-400)

        if [ -z "$REPLACE_OUTPUT" ]; then
            REPLACE_OUTPUT="Remote replacement failed (exit code $REPLACE_EXIT)"
        fi

        # If replacement failed before mv, remove the temporary file.
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o LogLevel=ERROR \\
            -p "$PORT" "$COUNTER_USER@$IP" \\
            "rm -f $(printf '%q' "$REMOTE_TMP")" >/dev/null 2>&1 || true

        emit "$NUMBER" "$IP" "Failed" \\
            "Remote replacement failed: $REPLACE_OUTPUT" "$BACKUP"
        return 0
    fi

    # Grant the replacement file the same writable permissions as folder updates.
    PERMISSION_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" \\
            "sudo -S -p '' -k chmod 777 $(printf '%q' "$TARGET")" \\
            < "$PASSWORD_FILE" 2>&1
    )
    PERMISSION_EXIT=$?
    if [ "$PERMISSION_EXIT" -ne 0 ]; then
        PERMISSION_OUTPUT=$(printf '%s' "$PERMISSION_OUTPUT" | tr '\\n' ' ' | cut -c1-400)
        [ -z "$PERMISSION_OUTPUT" ] && PERMISSION_OUTPUT="sudo chmod failed (exit code $PERMISSION_EXIT)"
        emit "$NUMBER" "$IP" "Failed" \\
            "File replacement succeeded, but permission update failed: $PERMISSION_OUTPUT" "$BACKUP"
        return 0
    fi

    # ------------------------------------------------------------
    # 8. Final SHA-256 verification
    # ------------------------------------------------------------
    FINAL_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no \\
            -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" \\
            -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR \\
            -p "$PORT" \\
            "$COUNTER_USER@$IP" \\
            "sha256sum $(printf '%q' "$TARGET")" 2>&1
    )
    FINAL_EXIT=$?

    if [ "$FINAL_EXIT" -ne 0 ]; then
        FINAL_OUTPUT=$(printf '%s' "$FINAL_OUTPUT" | tr '\\n' ' ' | cut -c1-400)

        emit "$NUMBER" "$IP" "Failed" \\
            "Final SHA-256 check failed: $FINAL_OUTPUT" "$BACKUP"
        return 0
    fi

    FINAL_HASH=$(printf '%s\\n' "$FINAL_OUTPUT" | awk '{{print $1}}')

    if [ "$FINAL_HASH" != "$EXPECTED_HASH" ]; then
        emit "$NUMBER" "$IP" "Failed" \\
            "Final SHA-256 verification failed (expected $EXPECTED_HASH, got $FINAL_HASH)" "$BACKUP"
        return 0
    fi

    emit "$NUMBER" "$IP" "SUCCESS" \
        "File replaced and verified successfully" "$BACKUP"
}}

# ------------------------------------------------------------
# Counter list is embedded by the Windows controller.
#
# The BO does not need python3 to parse JSON. It only executes the
# standard shell/SSH/SCP tools required for distribution.
# ------------------------------------------------------------
COUNTER_LIST=(
{counter_block}
)

for LINE in "${{COUNTER_LIST[@]}}"; do
    IFS='|' read -r NUMBER IP SOURCE <<< "$LINE"
    [ -z "$NUMBER" ] && continue
    [ -z "$IP" ] && continue

    distribute_one "$NUMBER" "$IP" &
done

wait
exit 0
"""


    @staticmethod
    def _build_script(
        remote_file,
        remote_update,
        remote_manifest,
        remote_counter_script,
        remote_package,
        target_path,
        remote_password,
        remote_config,
        counter_username,
        counter_port,
        connect_timeout,
        operation_timeout,
        expected_hash,
        manifest_count,
        job_id,
        transfer_mode,
        counter_lines,
    ):
        if transfer_mode == "folder":
            return BOController._build_folder_script(
                remote_update=remote_update,
                remote_manifest=remote_manifest,
                remote_counter_script=remote_counter_script,
                remote_package=remote_package,
                target_path=target_path,
                remote_password=remote_password,
                remote_config=remote_config,
                counter_username=counter_username,
                counter_port=counter_port,
                connect_timeout=connect_timeout,
                operation_timeout=operation_timeout,
                manifest_count=manifest_count,
                job_id=job_id,
                counter_lines=counter_lines,
            )
        return BOController._build_file_script(
            remote_file=remote_file,
            target_path=target_path,
            remote_password=remote_password,
            remote_config=remote_config,
            counter_username=counter_username,
            counter_port=counter_port,
            connect_timeout=connect_timeout,
            operation_timeout=operation_timeout,
            expected_hash=expected_hash,
            counter_lines=counter_lines,
        )

    @staticmethod
    def _build_counter_folder_script():
        """Return the script executed directly on a Counter for a folder patch."""
        return """#!/usr/bin/env bash
set -u

STAGE=\"$1\"
MANIFEST=\"$2\"
TARGET=\"$3\"
BACKUP_ROOT=\"$4\"
EXPECTED_FILE_COUNT=\"$5\"
COUNTER_ROOT=\"$(dirname \"$STAGE\")\"

BACKED_UP_FILES=()
NEW_FILES=()
BACKUP_CREATED=0

rollback() {
    for REL_PATH in \"${BACKED_UP_FILES[@]}\"; do
        if [ -f \"$BACKUP_ROOT/$REL_PATH\" ]; then
            mkdir -p \"$(dirname \"$TARGET/$REL_PATH\")\" >/dev/null 2>&1 || true
            cp -p \"$BACKUP_ROOT/$REL_PATH\" \"$TARGET/$REL_PATH\" >/dev/null 2>&1 || true
        fi
    done
    for REL_PATH in \"${NEW_FILES[@]}\"; do
        rm -f \"$TARGET/$REL_PATH\" >/dev/null 2>&1 || true
    done
}

# Verify the staged files before changing the target.
COUNT=0
while IFS='|' read -r EXPECTED_HASH REL_PATH; do
    [ -z \"$REL_PATH\" ] && continue
    STAGED=\"$STAGE/$REL_PATH\"
    if [ ! -f \"$STAGED\" ] || [ -L \"$STAGED\" ]; then
        echo \"Missing staged file: $REL_PATH\" >&2
        exit 31
    fi
    ACTUAL_HASH=$(sha256sum \"$STAGED\" | awk '{print $1}')
    if [ \"$ACTUAL_HASH\" != \"$EXPECTED_HASH\" ]; then
        echo \"SHA-256 mismatch for $REL_PATH (expected $EXPECTED_HASH, got $ACTUAL_HASH)\" >&2
        exit 32
    fi
    COUNT=$((COUNT + 1))
done < \"$MANIFEST\"

if [ \"$COUNT\" -ne \"$EXPECTED_FILE_COUNT\" ]; then
    echo \"Manifest file count mismatch (expected $EXPECTED_FILE_COUNT, got $COUNT)\" >&2
    exit 33
fi

# Check the destination folder and all directory/file conflicts before modifying anything.
if [ -L \"$TARGET\" ]; then
    echo \"Target folder is a symbolic link: $TARGET\" >&2
    exit 40
fi
if [ -e \"$TARGET\" ] && [ ! -d \"$TARGET\" ]; then
    echo \"Target path exists but is not a directory: $TARGET\" >&2
    exit 40
fi

while IFS= read -r -d '' DIR; do
    [ \"$DIR\" = \"$STAGE\" ] && continue
    REL_PATH=\"${DIR#$STAGE/}\"
    TARGET_DIR=\"$TARGET/$REL_PATH\"
    if [ -L \"$TARGET_DIR\" ]; then
        echo \"Target path is a symbolic link where a directory is required: $REL_PATH\" >&2
        exit 41
    fi
    if [ -e \"$TARGET_DIR\" ] && [ ! -d \"$TARGET_DIR\" ]; then
        echo \"Target path is a file where a directory is required: $REL_PATH\" >&2
        exit 42
    fi
done < <(find \"$STAGE\" -type d -print0)

while IFS='|' read -r EXPECTED_HASH REL_PATH; do
    [ -z \"$REL_PATH\" ] && continue
    TARGET_FILE=\"$TARGET/$REL_PATH\"
    if [ -L \"$TARGET_FILE\" ]; then
        echo \"Target file is a symbolic link and will not be replaced: $REL_PATH\" >&2
        exit 43
    fi
    if [ -e \"$TARGET_FILE\" ] && [ ! -f \"$TARGET_FILE\" ]; then
        echo \"Target path is not a regular file: $REL_PATH\" >&2
        exit 44
    fi
done < \"$MANIFEST\"

# Create the selected folder under its parent when it does not exist.
mkdir -p \"$TARGET\" || {
    echo \"Could not create target folder: $TARGET\" >&2
    rollback
    exit 50
}

# Create missing directories first. Existing directories are preserved.
while IFS= read -r -d '' DIR; do
    [ \"$DIR\" = \"$STAGE\" ] && continue
    REL_PATH=\"${DIR#$STAGE/}\"
    mkdir -p \"$TARGET/$REL_PATH\" || {
        echo \"Could not create target directory: $REL_PATH\" >&2
        rollback
        exit 51
    }
done < <(find \"$STAGE\" -type d -print0)

# Apply each file. Existing files are backed up; new files are not.
while IFS='|' read -r EXPECTED_HASH REL_PATH; do
    [ -z \"$REL_PATH\" ] && continue

    STAGED=\"$STAGE/$REL_PATH\"
    TARGET_FILE=\"$TARGET/$REL_PATH\"

    if [ -f \"$TARGET_FILE\" ]; then
        if [ \"$BACKUP_CREATED\" -eq 0 ]; then
            mkdir -p \"$BACKUP_ROOT\" || {
                echo \"Could not create backup directory: $BACKUP_ROOT\" >&2
                rollback
                exit 52
            }
            BACKUP_CREATED=1
        fi

        mkdir -p \"$BACKUP_ROOT/$(dirname \"$REL_PATH\")\" || {
            echo \"Could not create backup subdirectory for: $REL_PATH\" >&2
            rollback
            exit 53
        }

        cp -p \"$TARGET_FILE\" \"$BACKUP_ROOT/$REL_PATH\" || {
            echo \"Could not back up existing file: $REL_PATH\" >&2
            rollback
            exit 54
        }
        BACKED_UP_FILES+=(\"$REL_PATH\")
        OLD_MODE=$(stat -c '%a' \"$TARGET_FILE\") || OLD_MODE=\"\"
    else
        OLD_MODE=\"\"
    fi

    TMP_TARGET=\"$TARGET_FILE.posdist_tmp_$$\"
    rm -f \"$TMP_TARGET\" >/dev/null 2>&1 || true

    cp -p \"$STAGED\" \"$TMP_TARGET\" || {
        echo \"Could not stage file on target: $REL_PATH\" >&2
        rm -f \"$TMP_TARGET\" >/dev/null 2>&1 || true
        rollback
        exit 55
    }

    if [ -n \"$OLD_MODE\" ]; then
        chmod \"$OLD_MODE\" \"$TMP_TARGET\" || {
            echo \"Could not preserve permissions for: $REL_PATH\" >&2
            rm -f \"$TMP_TARGET\" >/dev/null 2>&1 || true
            rollback
            exit 56
        }
    fi

    TMP_HASH=$(sha256sum \"$TMP_TARGET\" | awk '{print $1}') || {
        echo \"Could not hash temporary target file: $REL_PATH\" >&2
        rm -f \"$TMP_TARGET\" >/dev/null 2>&1 || true
        rollback
        exit 57
    }

    if [ \"$TMP_HASH\" != \"$EXPECTED_HASH\" ]; then
        echo \"Temporary SHA-256 mismatch for $REL_PATH\" >&2
        rm -f \"$TMP_TARGET\" >/dev/null 2>&1 || true
        rollback
        exit 58
    fi

    mv -f \"$TMP_TARGET\" \"$TARGET_FILE\" || {
        echo \"Could not replace target file: $REL_PATH\" >&2
        rm -f \"$TMP_TARGET\" >/dev/null 2>&1 || true
        rollback
        exit 59
    }

    if [ -z \"$OLD_MODE\" ]; then
        NEW_FILES+=(\"$REL_PATH\")
    fi

    FINAL_HASH=$(sha256sum \"$TARGET_FILE\" | awk '{print $1}') || {
        echo \"Final SHA-256 calculation failed for $REL_PATH\" >&2
        rollback
        exit 60
    }

    if [ \"$FINAL_HASH\" != \"$EXPECTED_HASH\" ]; then
        echo \"Final SHA-256 verification failed for $REL_PATH (expected $EXPECTED_HASH, got $FINAL_HASH)\" >&2
        rollback
        exit 61
    fi
done < \"$MANIFEST\"

rm -rf \"$COUNTER_ROOT\" >/dev/null 2>&1 || true

if [ \"$BACKUP_CREATED\" -eq 1 ]; then
    printf 'BACKUP\\t%s\\n' \"$BACKUP_ROOT\"
fi
printf 'UPDATED\\t%s\\n' \"$EXPECTED_FILE_COUNT\"
"""

    @staticmethod
    def _build_folder_script(
        remote_update,
        remote_manifest,
        remote_counter_script,
        remote_package,
        target_path,
        remote_password,
        remote_config,
        counter_username,
        counter_port,
        connect_timeout,
        operation_timeout,
        manifest_count,
        job_id,
        counter_lines,
    ):
        counter_records = [f"{number}|{ip}|{source}" for number, ip, source in counter_lines]
        counter_block = "\n".join("    " + shlex.quote(record) for record in counter_records)

        return f"""#!/usr/bin/env bash
set -u

REMOTE_UPDATE={shlex.quote(remote_update)}
REMOTE_MANIFEST={shlex.quote(remote_manifest)}
REMOTE_COUNTER_SCRIPT={shlex.quote(remote_counter_script)}
REMOTE_PACKAGE={shlex.quote(remote_package)}
TARGET={shlex.quote(target_path)}
PASSWORD_FILE={shlex.quote(remote_password)}
COUNTERS_FILE={shlex.quote(remote_config)}
COUNTER_USER={shlex.quote(counter_username)}
PORT={counter_port}
CONNECT_TIMEOUT={connect_timeout}
OP_TIMEOUT={operation_timeout}
JOB_ID={shlex.quote(job_id)}
EXPECTED_FILE_COUNT={manifest_count}

emit() {{
    printf 'RESULT\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \"$1\" \"$2\" \"$3\" \"$4\" \"$5\"
}}

for REQUIRED in sshpass scp ssh timeout sha256sum tar; do
    if ! command -v \"$REQUIRED\" >/dev/null 2>&1; then
        emit \"BO\" \"0.0.0.0\" \"Failed\" \"$REQUIRED is not installed on BO\" \"\"
        exit 2
    fi
done

distribute_one() {{
    NUMBER=\"$1\"
    IP=\"$2\"
    COUNTER_ROOT=\"/tmp/pos_file_distributor/$JOB_ID\"
    COUNTER_STAGE=\"$COUNTER_ROOT/update\"
    COUNTER_MANIFEST="$COUNTER_ROOT/manifest.txt"
    COUNTER_APPLY="$COUNTER_ROOT/counter_folder_apply.sh"
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_ROOT=\"${{TARGET}}.posdist_backup_${{TIMESTAMP}}_${{JOB_ID}}\"

    SSH_CHECK_OUTPUT=$(
        timeout \"$CONNECT_TIMEOUT\" sshpass -f \"$PASSWORD_FILE\" ssh \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout=\"$CONNECT_TIMEOUT\" -o ConnectionAttempts=1 \\
            -o BatchMode=no -o LogLevel=ERROR -p \"$PORT\" \\
            \"$COUNTER_USER@$IP\" \"true\" 2>&1
    )
    SSH_CHECK_EXIT=$?
    if [ \"$SSH_CHECK_EXIT\" -ne 0 ]; then
        SSH_CHECK_OUTPUT=$(printf '%s' \"$SSH_CHECK_OUTPUT\" | tr '\\n' ' ' | cut -c1-300)
        [ -z \"$SSH_CHECK_OUTPUT\" ] && SSH_CHECK_OUTPUT=\"SSH connection failed (exit code $SSH_CHECK_EXIT)\"
        emit \"$NUMBER\" \"$IP\" \"Device Off\" \"$SSH_CHECK_OUTPUT\" \"\"
        return 0
    fi

    PARENT_TARGET=$(dirname \"$TARGET\")
    TARGET_Q=$(printf '%q' \"$PARENT_TARGET\")
    DIR_OUTPUT=$(
        timeout \"$OP_TIMEOUT\" sshpass -f \"$PASSWORD_FILE\" ssh \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout=\"$CONNECT_TIMEOUT\" -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR -p \"$PORT\" \"$COUNTER_USER@$IP\" \\
            \"test -d $TARGET_Q\" 2>&1
    )
    DIR_EXIT=$?
    if [ \"$DIR_EXIT\" -ne 0 ]; then
        DIR_OUTPUT=$(printf '%s' \"$DIR_OUTPUT\" | tr '\\n' ' ' | cut -c1-300)
        [ -z \"$DIR_OUTPUT\" ] && DIR_OUTPUT=\"Target folder does not exist or is not accessible\"
        emit \"$NUMBER\" \"$IP\" \"Failed\" \"$DIR_OUTPUT\" \"\"
        return 0
    fi

    WRITE_TEST=\"$PARENT_TARGET/.posdist_write_test_$JOB_ID\"
    WRITE_OUTPUT=$(
        timeout \"$OP_TIMEOUT\" sshpass -f \"$PASSWORD_FILE\" ssh \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout=\"$CONNECT_TIMEOUT\" -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR -p \"$PORT\" \"$COUNTER_USER@$IP\" \\
            \"touch $(printf '%q' \"$WRITE_TEST\") && rm -f $(printf '%q' \"$WRITE_TEST\")\" 2>&1
    )
    WRITE_EXIT=$?
    if [ \"$WRITE_EXIT\" -ne 0 ]; then
        WRITE_OUTPUT=$(printf '%s' \"$WRITE_OUTPUT\" | tr '\\n' ' ' | cut -c1-300)
        [ -z \"$WRITE_OUTPUT\" ] && WRITE_OUTPUT=\"Target folder is not writable\"
        emit \"$NUMBER\" \"$IP\" \"Failed\" \"Target folder not writable: $WRITE_OUTPUT\" \"\"
        return 0
    fi

    PREP_OUTPUT=$(
        timeout \"$OP_TIMEOUT\" sshpass -f \"$PASSWORD_FILE\" ssh \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout=\"$CONNECT_TIMEOUT\" -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR -p \"$PORT\" \"$COUNTER_USER@$IP\" \\
            \"rm -rf $(printf '%q' \"$COUNTER_ROOT\") && mkdir -p $(printf '%q' \"$COUNTER_STAGE\")\" 2>&1
    )
    PREP_EXIT=$?
    if [ \"$PREP_EXIT\" -ne 0 ]; then
        PREP_OUTPUT=$(printf '%s' \"$PREP_OUTPUT\" | tr '\\n' ' ' | cut -c1-400)
        emit \"$NUMBER\" \"$IP\" \"Failed\" \"Could not prepare counter staging area: $PREP_OUTPUT\" \"\"
        return 0
    fi

    SCP_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" scp -q \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR -P "$PORT" "$REMOTE_PACKAGE" \\
            "$COUNTER_USER@$IP:$COUNTER_ROOT/update_package.tar.gz" 2>&1
    )
    SCP_EXIT=$?
    if [ "$SCP_EXIT" -ne 0 ]; then
        SCP_OUTPUT=$(printf '%s' "$SCP_OUTPUT" | tr '\\n' ' ' | cut -c1-400)
        [ -z "$SCP_OUTPUT" ] && SCP_OUTPUT="SCP folder package upload failed (exit code $SCP_EXIT)"
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout="$CONNECT_TIMEOUT" -o LogLevel=ERROR -p "$PORT" "$COUNTER_USER@$IP" "rm -rf $(printf '%q' "$COUNTER_ROOT")" >/dev/null 2>&1 || true
        emit "$NUMBER" "$IP" "Failed" "SCP folder package upload failed: $SCP_OUTPUT" ""
        return 0
    fi

    EXTRACT_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout="$CONNECT_TIMEOUT" -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR -p "$PORT" "$COUNTER_USER@$IP" \\
            "tar -xzf $(printf '%q' "$COUNTER_ROOT/update_package.tar.gz") -C $(printf '%q' "$COUNTER_ROOT") && rm -f $(printf '%q' "$COUNTER_ROOT/update_package.tar.gz")" 2>&1
    )
    EXTRACT_EXIT=$?
    if [ "$EXTRACT_EXIT" -ne 0 ]; then
        EXTRACT_OUTPUT=$(printf '%s' "$EXTRACT_OUTPUT" | tr '\\n' ' ' | cut -c1-400)
        [ -z "$EXTRACT_OUTPUT" ] && EXTRACT_OUTPUT="Could not extract folder update package (exit code $EXTRACT_EXIT)"
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout="$CONNECT_TIMEOUT" -o LogLevel=ERROR -p "$PORT" "$COUNTER_USER@$IP" "rm -rf $(printf '%q' "$COUNTER_ROOT")" >/dev/null 2>&1 || true
        emit "$NUMBER" "$IP" "Failed" "Folder package extraction failed: $EXTRACT_OUTPUT" ""
        return 0
    fi

    # The helper script, manifest, and update tree are unpacked from one
    # compressed package. The helper performs verification, backups, recursive
    # directory creation, replacement,
    # and rollback logic locally on the counter.
    APPLY_OUTPUT=$(
        timeout \"$OP_TIMEOUT\" sshpass -f \"$PASSWORD_FILE\" ssh \\
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
            -o ConnectTimeout=\"$CONNECT_TIMEOUT\" -o ConnectionAttempts=1 \\
            -o LogLevel=ERROR -p \"$PORT\" \"$COUNTER_USER@$IP\" \\
            \"bash $(printf '%q' \"$COUNTER_APPLY\") $(printf '%q' \"$COUNTER_STAGE\") $(printf '%q' \"$COUNTER_MANIFEST\") $(printf '%q' \"$TARGET\") $(printf '%q' \"$BACKUP_ROOT\") $EXPECTED_FILE_COUNT\" 2>&1
    )
    APPLY_EXIT=$?

    if [ \"$APPLY_EXIT\" -ne 0 ]; then
        APPLY_OUTPUT=$(printf '%s' \"$APPLY_OUTPUT\" | tr '\\n' ' ' | cut -c1-600)
        # Do not delete the backup directory. It is outside COUNTER_ROOT and
        # remains available for manual recovery. Only temporary staging is removed.
        timeout \"$OP_TIMEOUT\" sshpass -f \"$PASSWORD_FILE\" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=\"$CONNECT_TIMEOUT\" -o LogLevel=ERROR -p \"$PORT\" \"$COUNTER_USER@$IP\" \"rm -rf $(printf '%q' \"$COUNTER_ROOT\")\" >/dev/null 2>&1 || true
        emit \"$NUMBER\" \"$IP\" \"Failed\" \"Folder update failed: $APPLY_OUTPUT\" \"$BACKUP_ROOT\"
        return 0
    fi

    # ------------------------------------------------------------
    # 9. Update permissions on the destination folder only.
    #    Example:
    #      TARGET=/home/countertest/suite/Test
    #      PERMISSION_TARGET=/home/countertest/suite
    #
    #    Use the same Counter password already stored in PASSWORD_FILE
    #    for sudo authentication.
    # ------------------------------------------------------------
    PERMISSION_TARGET="$TARGET"

    if [ -z "$PERMISSION_TARGET" ] || [ "$PERMISSION_TARGET" = "/" ]; then
        emit "$NUMBER" "$IP" "Failed" \
            "Permission target is unsafe: $PERMISSION_TARGET" "$BACKUP_ROOT"
        return 0
    fi

    PERMISSION_Q=$(printf '%q' "$PERMISSION_TARGET")

    PERMISSION_OUTPUT=$(
        timeout "$OP_TIMEOUT" sshpass -f "$PASSWORD_FILE" ssh \
            -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout="$CONNECT_TIMEOUT" -o ConnectionAttempts=1 \
            -o LogLevel=ERROR -p "$PORT" "$COUNTER_USER@$IP" \
            "sudo -S -p '' -k chmod 777 -R $PERMISSION_Q" \
            < "$PASSWORD_FILE" 2>&1
    )
    PERMISSION_EXIT=$?

    if [ "$PERMISSION_EXIT" -ne 0 ]; then
        PERMISSION_OUTPUT=$(printf '%s' "$PERMISSION_OUTPUT" | tr '\\n' ' ' | cut -c1-600)
        [ -z "$PERMISSION_OUTPUT" ] && PERMISSION_OUTPUT="sudo chmod failed (exit code $PERMISSION_EXIT)"

        emit "$NUMBER" "$IP" "Failed" \
            "Folder update succeeded, but permission update failed for $PERMISSION_TARGET: $PERMISSION_OUTPUT" \
            "$BACKUP_ROOT"
        return 0
    fi

    BACKUP_PATH=$(printf '%s\\n' \"$APPLY_OUTPUT\" | awk -F '\\t' '/^BACKUP\\t/{{print $2; exit}}')
    PROCESSED_COUNT=$(printf '%s\\n' \"$APPLY_OUTPUT\" | awk -F '\\t' '/^UPDATED\\t/{{print $2; exit}}')
    [ -z \"$PROCESSED_COUNT\" ] && PROCESSED_COUNT=\"$EXPECTED_FILE_COUNT\"
    emit \"$NUMBER\" \"$IP\" \"SUCCESS\" \"Folder update completed and verified ($PROCESSED_COUNT file(s) processed)\" \"$BACKUP_PATH\"
}}

COUNTER_LIST=(
{counter_block}
)

for LINE in \"${{COUNTER_LIST[@]}}\"; do
    IFS='|' read -r NUMBER IP SOURCE <<< \"$LINE\"
    [ -z \"$NUMBER\" ] && continue
    [ -z \"$IP\" ] && continue
    distribute_one \"$NUMBER\" \"$IP\" &
done

wait
exit 0
"""
    @staticmethod
    def _parse_results(output):
        results = []

        for line in output.splitlines():
            if not line.startswith("RESULT\t"):
                continue

            parts = line.split("\t", 5)

            if len(parts) < 6:
                continue

            _, number, ip, status, message, backup = parts

            try:
                number_value = int(number)
            except ValueError:
                number_value = number

            results.append(
                {
                    "counter": number_value,
                    "ip": ip,
                    "status": status,
                    "message": message,
                    "backup": backup,
                }
            )

        return sorted(
            results,
            key=lambda x: (
                int(x["counter"])
                if str(x["counter"]).isdigit()
                else 999999
            ),
        )
