# SPDX-License-Identifier: GPL-3.0-or-later

# File for keeping track of and editing kaithem modules(not python modules)
import copy
import gc
import glob
import json
import logging
import os
import re
import shutil
import time
import traceback
import urllib.parse
import uuid
import weakref
import zipfile
from io import BytesIO
from typing import Any

import structlog
import yaml

from kaithem.src.validation_util import validate_args

from . import auth, directories, messagebus, modules_state, pages, util
from .modules_state import (
    ResourceDictType,
    check_forbidden,
    external_module_locations,
    getModuleDir,
    getModuleFn,
    modulesLock,
    mutable_copy_resource,
    resource_types,
    saveModule,
    scopes,
    special_resources,
)

logger = structlog.get_logger(__name__)


def loadAllCustomResourceTypes() -> None:
    # TODO this is O(m * n) time. Is that bad?
    start_time = time.time()

    types: list[tuple[float, str]] = []
    for key, typeobj in resource_types.items():
        types.append((typeobj.priority, key))
    types = sorted(types)

    for loading_priority, loading_rt in types:
        for i in modules_state.ActiveModules:
            # Ensure that child elements load before parent elements.
            for j in sorted(
                list(modules_state.ActiveModules[i].keys()),
                key=lambda n: len(n),
                reverse=True,
            ):
                orig = modules_state.ActiveModules[i][j]
                # Be defensive in case something on loader code
                # Tries to modify something.  Note that modifying it isn't
                # actually supported.
                r = copy.deepcopy(orig)
                if hasattr(r, "get"):
                    if r.get("resource", {}).get("type") == loading_rt:
                        start_time_inner = time.time()
                        try:
                            rt = r["resource"]["type"]
                            assert isinstance(rt, str)
                            resource_types[rt]._validate(r)
                            resource_types[rt].on_load(i, j, r)
                        except Exception:
                            messagebus.post_message(
                                "/system/notifications/errors",
                                f"Error loading resource:{str((i, j))}",
                            )
                            logger.exception(
                                f"Error loading resource: {str((i, j))}"
                            )
                        taken = round(time.time() - start_time_inner, 2)
                        if taken > 1:
                            logger.info(f"Loading {i}:{j} took {taken}s")
                if not r == orig:
                    logger.warning(
                        f"Tried to modify resource object {i}:{j} during load"
                    )

    for i in resource_types:
        resource_types[i].on_finished_loading(None)

    taken = round(time.time() - start_time, 2)
    logger.info(f"Loaded module resources in {taken}s")


class ModuleObject:
    """
    These are the objects acessible as 'module' within pages, events, etc.
    Normally you use them to share variables,
    but they have incomplete and undocumented support
    For acting as an API for user code to acess or modify
     the resources, which could be useful if you want to be able to
    dynamically create resources, or more likely just acess
     file resource contents or metadata about the module.
    """

    def __init__(self, modulename: str) -> None:
        self.__kaithem_modulename__ = modulename


@validate_args
def readResourceFromFile(
    fn: str, relative_name: str, ver: int = 1, modulename: str | None = None
) -> tuple[ResourceDictType | None, str | None]:
    """Relative name is rel to the folder,
    aka the part of the path that actually belongs in
    the resource name.

    Modulename is there because this function will one day
     auto-migrate to new versions of the file
    format.
    """
    with open(fn, "rb") as f:
        d = f.read().decode("utf-8")

    x = readResourceFromData(d, relative_name, ver, filename=fn)
    # logger.debug("Loaded resource from file "+fn)
    original = copy.deepcopy(x[0])

    if not (x[0] == original):
        logger.info(
            f"Resource {x[1]} is in an older format and should be migrated"
        )
    # For now don't break anything by actually changing the data.
    return (original, x[1])


# Backwards compatible resource loader.
def readResourceFromData(
    d: str, relative_name: str, ver: int = 1, filename: str | None = None
) -> tuple[ResourceDictType | None, str | None]:
    """Returns (datadict, ResourceName)
    Should be pure except logging
    """
    fn = relative_name
    r = None
    if filename and (not filename.endswith((".yaml", ".toml", ".json"))):
        return None, None
    try:
        # This regex is meant to handle any combination of cr, lf,
        # and trailing whitespaces
        # We don't do anything with more that 3 sections yet,
        # so limit just in case there's ----
        # in a markdown file
        sections = re.split(r"\r?\n?----*\s*\r?\n*", d, maxsplit=2)

        shouldRemoveExtension = False

        isSpecialEncoded = False
        wasProblem = False

        if fn.endswith((".yaml", ".json", ".toml")):
            shouldRemoveExtension = True

        if not isSpecialEncoded:
            if fn.endswith(".toml"):
                import tomllib

                r = tomllib.loads(d)
            else:
                r = yaml.load(sections[0], Loader=yaml.SafeLoader)

            # Catch new style save files
            if len(sections) > 1:
                if r["resource"]["type"] == "page":
                    r["body"] = sections[1]

        if wasProblem:
            messagebus.post_message(
                "/system/notifications/warnings",
                f"Potential problem or nonstandard encoding with file: {fn}",
            )
    except Exception:
        # This is a workaround for when dolphin puts
        # .directory files in directories and gitignore files
        # and things like that. Also ignore attempts to load from filedata
        # I'd like to add more workarounds
        # if there are other programs that insert similar crap files.
        if (
            "/.git" in fn
            or "/.gitignore" in fn
            or "__filedata__" in fn
            or fn.endswith(".directory")
        ):
            return (None, None)
        else:
            raise

    if r:
        r = modules_state.normalize_resource_data(r)

    if not r or not r["resource"] or "type" not in r["resource"]:
        if (
            "/.git" in fn
            or "/.gitignore" in fn
            or "__filedata__" in fn
            or fn.endswith(".directory")
        ):
            return None, None
        else:
            print(fn)

    assert r

    resourcename = util.unurl(fn)
    if shouldRemoveExtension:
        resourcename = ".".join(resourcename.split(".")[:-1])
    return (r, resourcename)


def initModules() -> None:
    global external_module_locations
    """"Find the most recent module dump folder and use that.
    Should there not be a module dump folder, it is corrupted, etc,
    Then start with an empty list of modules.
    Should normally be called once at startup."""

    if not os.path.isdir(directories.moduledir):
        os.makedirs(directories.moduledir, exist_ok=True)

    try:
        # Finish or undo any module replacement interrupted by a crash
        # or killed process before we load anything.
        recover_pending_module_uploads()

        # __COMPLETE__ is a special file we write to the
        # dump directory to show it as valid
        possibledir = os.path.join(directories.moduledir, "data")
        if os.path.isdir(possibledir):
            loadModules(possibledir)

    except Exception:
        logging.exception("Err loading modules")
        messagebus.post_message(
            "/system/notifications/errors",
            f" Error loading modules: {traceback.format_exc(4)}",
        )

    auth.importPermissionsFromModules()
    loadAllCustomResourceTypes()
    modules_state.moduleshash = modules_state.hashModules()
    logger.info("Initialized modules")


def loadModules(modulesdir: str) -> None:
    "Load all modules in the given folder to RAM."
    logger.debug(f"Loading modules from {modulesdir}")
    for i in util.get_immediate_subdirectories(modulesdir):
        loadModule(os.path.join(modulesdir, i), util.unurl(i))

    for i in os.listdir(modulesdir):
        try:
            if not i.endswith(".location"):
                continue
            if not os.path.isfile(os.path.join(modulesdir, i)):
                continue
            # Read ythe location we are supposed to load from
            with open(os.path.join(modulesdir, i)) as f:
                s = f.read(4096)
            # Get rid of the .location, then set the location in the dict
            with modulesLock:
                external_module_locations[util.unurl(i[0:-9])] = s
            # We use the ignore func when loading ext modules
            loadModule(s, util.unurl(i[0:-9]))
        except Exception:
            messagebus.post_message(
                "/system/notifications/errors",
                f" Error loading external module: {traceback.format_exc(4)}",
            )


def ignore_func(path: str) -> bool:
    """Recursive detect paths that should be ignored
    and left alone when loading and saving"""
    # Safety counter, this seems like it might need it.
    for i in range(64):
        if _detect_ignorable(path):
            return True
        path = os.path.dirname(path)
        # Detect end of path
        if not os.path.split(path)[1]:
            return False
    return False


def _detect_ignorable(path: str) -> bool:
    "Detect paths that should be ignored when loading a module"
    bn = os.path.basename(path)
    parts = bn.split(os.path.sep)

    if bn.startswith("."):
        return True

    if bn.startswith("__"):
        if bn.split(".")[0] not in special_resources:
            return True

    if "__filedata__" in parts:
        return True

    return False


def _scan_module_folder(
    folder: str,
    modulename: str,
    resource_folder: str | None = None,
    strict: bool = False,
) -> dict[str, ResourceDictType]:
    """Read every resource in a module folder and return the resource
    dict, without registering the module anywhere.

    In strict mode, the first resource that fails to parse or validate
    raises instead of being skipped with an error notification. This is
    used to fully vet an uploaded module before it is allowed to take
    over from a currently running one."""
    logger.debug(f"Scanning module folder {modulename}: {folder}")

    if not resource_folder:
        resource_folder = os.path.join(folder, "__filedata__")

    # Make an empty dict to hold the module resources
    module: dict[str, ResourceDictType] = {}

    for t in resource_types:
        try:
            found = resource_types[t].scan_dir(folder)
        except Exception:
            if strict:
                raise
            messagebus.post_message(
                "/system/notifications/errors",
                f"Loading resource type {t}: {traceback.format_exc(4)}",
            )
        else:
            for rn in found:
                rsc = modules_state.normalize_resource_data(found[rn])
                module[rn] = rsc

    # Iterate over all resource files and load them
    for root, dirs, files in os.walk(folder):
        # Function used to ignore things like VCS folders and such
        if ignore_func(root):
            continue

        if root.startswith(resource_folder):
            continue

        # Handle all resources that have unusual file types.

        # TODO multiple storage types for one
        # Name mean we can have conflicts, detect and warn
        for i in dirs:
            if not ignore_func(i):
                for t in resource_types:
                    abs = os.path.join(root, i)
                    rel = os.path.relpath(abs, folder)
                    try:
                        found = resource_types[t].scan_dir(abs)
                    except Exception:
                        if strict:
                            raise
                        messagebus.post_message(
                            "/system/notifications/errors",
                            f"resource type {t}: {traceback.format_exc(4)}",
                        )
                    else:
                        found = copy.deepcopy(found)
                        for rn in found:
                            rsc = modules_state.normalize_resource_data(
                                found[rn]
                            )
                            if rel:
                                module[rel + "/" + rn] = rsc
                            else:
                                module[rn] = rsc

        for i in files:
            # For testing
            if ".fail_to_load_this_module_instantly" in i:
                raise RuntimeError(
                    f"File {i} in module {modulename} is a fail to load"
                )

            relfn = os.path.relpath(os.path.join(root, i), folder)
            fn = os.path.join(folder, relfn)
            if ignore_func(i):
                continue

            if "/." in fn:
                continue

            if fn.endswith((".yaml", ".json", ".toml")):
                try:
                    r, resourcename = readResourceFromFile(fn, relfn)
                    if not r or not resourcename:
                        # File managers sprinkle this crap around
                        if not os.path.basename(fn) == ".directory":
                            logger.exception(f"Null loading {fn}")
                        continue

                    module[resourcename] = r
                    if "resource" not in r or "type" not in r["resource"]:
                        logger.warning(
                            f"No resource type found for {resourcename}"
                        )
                        continue

                except Exception:
                    if strict:
                        raise
                    messagebus.post_message(
                        "/system/notifications/errors",
                        f"Error loading: {fn}\r\n{traceback.format_exc()}",
                    )

        for i in dirs:
            if ignore_func(i):
                continue
            relfn = os.path.relpath(os.path.join(root, i), folder)

            # Create a directory resource for the dirrctory
            module[util.unurl(relfn)] = {"resource": {"type": "directory"}}

    if "__metadata__" not in module:
        module["__metadata__"] = {
            "resource": {
                "type": "module_metadata",
                "modified": int(time.time()),
            },
            "description": "",
        }

    if strict:
        # Fully vet every resource before the module can take over.
        for rn, r in list(module.items()):
            rt = r.get("resource", {}).get("type")
            if isinstance(rt, str) and rt in resource_types:
                resource_types[rt]._validate(r)

    return module


def loadModule(
    folder: str,
    modulename: str,
    resource_folder: str | None = None,
    notify: bool = True,
) -> None:
    """Load a single module but don't bookkeep it
     and actually init everything with resource types.
    Used by loadModules.

    notify=False suppresses the /system/modules/loaded message, for use
    by transactional switch code that posts exactly one itself."""
    with modulesLock:
        module = _scan_module_folder(
            folder, modulename, resource_folder=resource_folder
        )

        scopes[modulename] = ModuleObject(modulename)
        modules_state.ActiveModules[modulename] = module
        modules_state.importFiledataFolderStructure(modulename)
        if notify:
            messagebus.post_message("/system/modules/loaded", modulename)


# Folders holding in-flight upload state, siblings of the live "data"
# directory so a normal module scan can never pick them up. Everything
# here lives on the same filesystem as the module data, so directory
# renames into place are atomic.
UPLOAD_TXN_DIRNAME = "__txn__"
LEGACY_UPLOAD_DIRNAME = "__upload__"
LEGACY_BACKUP_DIRNAME = "__backup__"


def _module_data_root() -> str:
    return os.path.join(directories.moduledir, "data")


def _upload_txn_root() -> str:
    return os.path.join(directories.moduledir, UPLOAD_TXN_DIRNAME)


def _safe_rmtree(path: str, base: str) -> None:
    """rmtree with a paranoid containment check."""
    path = os.path.abspath(path)
    base = os.path.abspath(base)
    if path == base or not path.startswith(base + os.path.sep):
        raise AssertionError(f"Refusing to remove {path}, outside {base}")
    shutil.rmtree(path)


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)
    _fsync_dir(os.path.dirname(path))


def _txn_marker_path(tdir: str, module: str) -> str:
    safe = urllib.parse.quote(module, safe="")
    return os.path.join(tdir, f"marker-{safe}.json")


def _txn_has_markers(tdir: str) -> bool:
    return bool(glob.glob(os.path.join(tdir, "marker-*.json")))


def _quiesce_module_runtime(module: str) -> dict[str, ResourceDictType] | None:
    """Remove a running module from memory and unload all of its runtime
    objects, but do NOT touch its files, permissions, hashes, or post any
    lifecycle notifications. Used to quietly unload the old version right
    before a validated new version takes over."""
    with modulesLock:
        if module not in modules_state.ActiveModules:
            return None
        x = modules_state.ActiveModules.pop(module)
        j = {
            i: copy.deepcopy(x[i])
            for i in x
            if not isinstance(x[i], weakref.ref)
        }
        scopes.pop(module, None)

    for i in resource_types:
        resource_types[i].on_delete_module(module)

    for k in j:
        rt = j[k].get("resource", {}).get("type")
        if isinstance(rt, str) and rt in resource_types:
            try:
                resource_types[rt].on_unload(module, k, j[k])
            except Exception:
                logging.exception("Error deleting resource type")
                messagebus.post_message(
                    "/system/notifications/errors",
                    f"Error deleting resource: {module},{k}",
                )

    gc.collect()
    return j


def _rollback_module_upload(
    module: str,
    tdir: str,
    replacing: bool,
) -> None:
    """Undo a module switch that failed after the on-disk cutover.

    Runs with the modules lock held. The transaction marker is removed
    only once the previous version is fully serving again; otherwise it
    is left for startup recovery."""
    marker = _txn_marker_path(tdir, module)
    dest = os.path.join(_module_data_root(), module)
    old_backup = os.path.join(tdir, "old", module)

    _quiesce_module_runtime(module)

    restored = False
    try:
        if replacing:
            if os.path.isdir(dest):
                _safe_rmtree(dest, directories.moduledir)
            os.rename(old_backup, dest)
            loadModule(dest, module, notify=False)
            bookkeeponemodule(module, notify=False)
        else:
            if os.path.isdir(dest):
                _safe_rmtree(dest, directories.moduledir)
        restored = True
    finally:
        # Keep permissions and hashes consistent with whichever version
        # is now in ActiveModules. Only consume the marker once the
        # previous version is actually serving again.
        try:
            auth.importPermissionsFromModules()
            modules_state.recalcModuleHashes()
        finally:
            if restored and os.path.exists(marker):
                os.remove(marker)


def _commit_module_upload(
    module: str,
    staged: str,
    replace: bool,
    tdir: str,
) -> None:
    """Switch one extracted module folder in as a recoverable
    transaction.

    The old module keeps serving until the new content has been fully
    parsed and validated; disk state then changes via same-fs atomic
    renames and only afterwards is the runtime swapped. Any failure
    restores the previous version in memory and on disk."""
    dest = os.path.join(_module_data_root(), module)
    replacing = module in modules_state.ActiveModules

    # Reject before changing anything.
    if replacing:
        if "module_lock" in modules_state.get_module_metadata(module):
            raise PermissionError("Old module is locked")
        if not replace:
            raise RuntimeError(f"Module {module} already loaded")
        if module in external_module_locations:
            raise RuntimeError(f"Module {module} is an external module")

    # Fully parse and validate the new content while the old version is
    # completely untouched.
    _scan_module_folder(staged, module, strict=True)

    marker = _txn_marker_path(tdir, module)
    old_backup = os.path.join(tdir, "old", module)
    os.makedirs(os.path.join(tdir, "old"), exist_ok=True)
    _atomic_write_json(
        marker,
        {"version": 1, "module": module, "started": time.time()},
    )

    # Durable on-disk cutover and runtime switch are all under
    # rollback protection: the old runtime objects are only unloaded
    # after the atomic renames, and no other thread can observe the
    # intermediate state because the modules lock is held throughout.
    try:
        if replacing:
            os.rename(dest, old_backup)
        os.rename(staged, dest)
        _fsync_dir(_module_data_root())

        if replacing:
            _quiesce_module_runtime(module)

        loadModule(dest, module, notify=False)
        bookkeeponemodule(module, strict=True, notify=False)
    except Exception:
        logger.exception(
            "Uploaded module %s failed to initialize; rolling back", module
        )
        _rollback_module_upload(module, tdir, replacing)
        messagebus.post_message(
            "/system/notifications/errors",
            f"Uploaded version of module {module} failed to load; "
            "continued running the previous version",
        )
        raise

    # New version is fully live: permissions, hashes and notifications
    # must reflect only it.
    auth.importPermissionsFromModules()
    modules_state.recalcModuleHashes()
    messagebus.post_message("/system/modules/loaded", module)

    # Commit point: delete the marker before the old backup, so a crash
    # in between leaves only an orphan backup folder and startup
    # recovery will never discard the now-committed new version.
    os.remove(marker)
    _fsync_dir(tdir)
    if replacing and os.path.isdir(old_backup):
        _safe_rmtree(old_backup, tdir)


def load_modules_from_zip(f: BytesIO, replace: bool = False) -> None:
    """Given a zip file, import all modules found therein.

    Each module commits independently as a recoverable switch: its
    content is validated before anything is replaced, the on-disk
    cutover uses atomic renames under a durable transaction marker, and
    any failure (including a killed process) restores the previous
    version rather than leaving two versions mixed."""
    z = zipfile.ZipFile(f)
    tdir = os.path.join(
        _upload_txn_root(), f"upload-{os.getpid()}-{uuid.uuid4().hex}"
    )
    new_root = os.path.join(tdir, "new")
    os.makedirs(new_root)

    try:
        with modulesLock:
            z.extractall(new_root)

            # Snapshot before any folders get renamed into place.
            names = sorted(
                i
                for i in os.listdir(new_root)
                if os.path.isdir(os.path.join(new_root, i))
            )

            for i in names:
                _commit_module_upload(
                    i, os.path.join(new_root, i), replace, tdir
                )
    finally:
        z.close()
        # With no markers left, every module either committed or rolled
        # back and only extraction debris remains. If a marker survives,
        # a catastrophic failure occurred and startup recovery must
        # finish the rollback.
        if os.path.isdir(tdir) and not _txn_has_markers(tdir):
            _safe_rmtree(tdir, directories.moduledir)
            root = _upload_txn_root()
            try:
                if os.path.isdir(root) and not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass


def recover_pending_module_uploads() -> None:
    """Roll back module replacements interrupted by a crash or killed
    process. Must run at startup before the data directory is loaded.

    A surviving marker means the switch never reached its commit point:
    restore the previous version when one exists, otherwise discard the
    partially uploaded new module. Also cleans leftovers from older
    releases and any orphaned transaction folders."""
    os.makedirs(directories.moduledir, exist_ok=True)
    data_root = _module_data_root()

    txn_root = _upload_txn_root()
    if os.path.isdir(txn_root):
        for token in sorted(os.listdir(txn_root)):
            tdir = os.path.join(txn_root, token)
            if not os.path.isdir(tdir):
                continue

            for mp in sorted(glob.glob(os.path.join(tdir, "marker-*.json"))):
                module = None
                try:
                    with open(mp) as fh:
                        info = json.load(fh)
                    module = info.get("module")
                    if (
                        not module
                        or "/" in module
                        or ".." in module
                        or module.startswith(".")
                    ):
                        logger.error(
                            "Ignoring invalid module upload marker %s", mp
                        )
                        os.remove(mp)
                        continue

                    dest = os.path.join(data_root, module)
                    old_backup = os.path.join(tdir, "old", module)

                    if os.path.isdir(old_backup):
                        logger.warning(
                            "Interrupted replacement of module %s; "
                            "restoring previous version",
                            module,
                        )
                        if os.path.isdir(dest):
                            _safe_rmtree(dest, directories.moduledir)
                        os.makedirs(data_root, exist_ok=True)
                        os.rename(old_backup, dest)
                        messagebus.post_message(
                            "/system/notifications/errors",
                            f"Interrupted update of module {module}; "
                            "previous version restored",
                        )
                    else:
                        logger.warning(
                            "Interrupted new module upload for %s; discarding",
                            module,
                        )
                        if os.path.isdir(dest):
                            _safe_rmtree(dest, directories.moduledir)

                    os.remove(mp)
                except Exception:
                    logger.exception(
                        "Failed to recover interrupted module upload %s", mp
                    )

            try:
                if not _txn_has_markers(tdir):
                    _safe_rmtree(tdir, directories.moduledir)
            except Exception:
                logger.exception("Failed to clean transaction folder %s", tdir)

        try:
            if os.path.isdir(txn_root) and not os.listdir(txn_root):
                os.rmdir(txn_root)
        except OSError:
            pass

    # Leftovers from the pre-transactional upload implementation:
    # a surviving backup always meant an interrupted replacement.
    legacy_backup = os.path.join(directories.moduledir, LEGACY_BACKUP_DIRNAME)
    if os.path.isdir(legacy_backup):
        for module in sorted(os.listdir(legacy_backup)):
            old = os.path.join(legacy_backup, module)
            if not os.path.isdir(old):
                continue
            dest = os.path.join(data_root, module)
            try:
                if os.path.isdir(dest):
                    _safe_rmtree(dest, directories.moduledir)
                os.makedirs(data_root, exist_ok=True)
                os.rename(old, dest)
                logger.warning(
                    "Restored module %s from interrupted legacy upload",
                    module,
                )
            except Exception:
                logger.exception(
                    "Failed to restore legacy backup for module %s", module
                )
        try:
            _safe_rmtree(legacy_backup, directories.moduledir)
        except Exception:
            logger.exception("Failed to remove legacy upload backup folder")

    legacy_upload = os.path.join(directories.moduledir, LEGACY_UPLOAD_DIRNAME)
    if os.path.isdir(legacy_upload):
        try:
            _safe_rmtree(legacy_upload, directories.moduledir)
        except Exception:
            logger.exception("Failed to remove legacy upload temp folder")


def bookkeeponemodule(
    module: str,
    update: bool = False,
    strict: bool = False,
    notify: bool = True,
) -> None:
    """Given the name of one module that has been copied to
    modules_state.ActiveModules but nothing else,
    let the rest of the system know the module is there.

    In strict mode, a resource that fails to initialize raises instead
    of leaving the module partially initialized, so transactional
    uploads can roll back. notify=False suppresses the loaded message.
    """

    if module not in scopes:
        scopes[module] = ModuleObject(module)

    for i in modules_state.ActiveModules[module]:
        # Handle events separately due to dependency resolution logic
        try:
            handleResourceChange(module, i, newly_added=not update)
        except Exception:
            if strict:
                raise
            messagebus.post_message(
                "/system/notifications/errors", f"Failed to load  resource: {i}"
            )

    for i in modules_state.resource_types:
        modules_state.resource_types[i].on_finished_loading(module)

    if notify and not update:
        messagebus.post_message("/system/modules/loaded", module)


def mvResource(module: str, resource: str, to_module: str, to_resource: str):
    # Raise an error if the user ever tries
    # to move something somewhere that does not exist.
    new = to_resource.split("/")
    for i in new:
        check_forbidden(i)

    if resource in special_resources or to_resource in special_resources:
        raise ValueError("Cannot move special resource")

    if not (
        "/".join(new[:-1]) in modules_state.ActiveModules[to_module]
        or len(new) < 2
    ):
        raise ValueError("Invalid destination")
    if to_module not in modules_state.ActiveModules:
        raise ValueError("Invalid destination")
    # If something by the name of the directory we
    # are moving to exists but it is not a directory.
    # short circuit evaluating the len makes this
    #  clause ignore moves that are to the root of a module.
    if not (
        len(new) < 2
        or modules_state.ActiveModules[to_module]["/".join(new[:-1])][
            "resource"
        ]["type"]
        == "directory"
    ):
        raise ValueError("Invalid destination")

    obj: modules_state.ResourceDictType = modules_state.ActiveModules[module][
        resource
    ]
    rt = obj["resource"]["type"]

    assert isinstance(rt, str)

    mp = []
    dir = modules_state.get_resource_save_location(to_module, to_resource)

    for i in os.listdir(dir):
        if i.split(".", 1)[0] == resource:
            newfn = os.path.join(dir, i.replace(resource, to_resource))
            oldfn = os.path.join(dir, i)
            mp.append((oldfn, newfn))
            if os.path.exists(newfn):
                raise FileExistsError(newfn)

    modules_state.ActiveModules[to_module][to_resource] = (
        modules_state.ActiveModules[module][resource]
    )
    modules_state.set_resource_error(module, resource, None)
    modules_state.set_resource_error(to_module, to_resource, None)
    del modules_state.ActiveModules[module][resource]
    if rt in modules_state.resource_types:
        modules_state.resource_types[rt].on_move(
            module, resource, to_module, to_resource, obj
        )

    os.makedirs(dir, exist_ok=True)

    for i in mp:
        if not i[0] == i[1]:
            shutil.move(i[0], i[1])


def rmResource(
    module: str, resource: str, message: str = "Resource Deleted"
) -> None:
    """Delete one resource by name, message
    is an optional message explaining the change"""
    with modulesLock:
        if resource not in modules_state.ActiveModules[module]:
            fr = os.path.join(getModuleDir(module), "__filedata__", resource)
            if os.path.isfile(fr):
                os.remove(fr)
            else:
                logger.warning(
                    f"Resource {resource} not found in module {module}"
                )
            return

    try:
        r = modules_state.ActiveModules[module][resource]
        rt = r["resource"]["type"]
        assert isinstance(rt, str)
        if rt in modules_state.resource_types:
            modules_state.resource_types[rt].on_delete(module, resource, r)
        unload_resource(module, resource)
        modules_state.set_resource_error(module, resource, None)
        modules_state.rawDeleteResource(module, resource)

        if rt == "directory":
            # Directories are special, they can have the extra data file
            fn = getModuleDir(module)
            fn = os.path.join(fn, resource)

            if os.path.exists(fn):
                os.remove(fn)

        elif rt == "permission":
            auth.importPermissionsFromModules()

    except Exception:
        messagebus.post_message(
            "/system/modules/errors/unloading",
            f"Error deleting resource: {str((module, resource))}",
        )


def unload_resource(module: str, resource: str):
    r = modules_state.ActiveModules[module][resource]
    rt = r["resource"]["type"]
    assert isinstance(rt, str)

    if rt in resource_types:
        resource_types[rt].on_unload(module, resource, r)


def newModule(
    name: str,
    location: str | None = None,
    metadata_defaults: dict[str, Any] = {},
) -> None:
    """Create a new module by the supplied name,
    throwing an error if one already exists.
    If location exists, load from there.

    metadata_defaults is a dictionary of default values,
    ignored if loading an existing module
    """

    check_forbidden(name)
    already_exists = False

    # If there is no module by that name,
    # create a blank template and the scope obj
    with modulesLock:
        if location:
            external_module_locations[name] = os.path.expanduser(location)

        if name in modules_state.ActiveModules:
            raise RuntimeError("A module by that name already exists.")
        if location:
            if os.path.isfile(location):
                raise RuntimeError(
                    "Cannot create new module that would clobber existing file"
                )

            if os.path.isdir(location):
                if os.path.isfile(os.path.join(location, "__metadata__.yaml")):
                    already_exists = True

                loadModule(location, name)
            else:
                _r: modules_state.ResourceDictType = {
                    "__metadata__": {
                        "resource": {
                            "type": "module_metadata",
                            "modified": int(time.time()),
                        },
                        "description": "",
                    }
                }
                r = mutable_copy_resource(_r)

                for i in metadata_defaults:
                    r[i] = copy.deepcopy(metadata_defaults[i])

                modules_state.ActiveModules[name] = r
        else:
            _r: modules_state.ResourceDictType = {
                "__metadata__": {
                    "resource": {
                        "type": "module_metadata",
                        "modified": int(time.time()),
                    },
                    "description": "",
                }
            }
            r = mutable_copy_resource(_r)

            for i in metadata_defaults:
                r[i] = copy.deepcopy(metadata_defaults[i])

            modules_state.ActiveModules[name] = r

        if not already_exists:
            saveModule(modules_state.ActiveModules[name], name)

        bookkeeponemodule(name)
        # Go directly to the newly created module
        messagebus.post_message(
            "/system/notifications",
            f"User {pages.getAcessingUser()} Created Module {name}",
        )
        messagebus.post_message(
            "/system/modules/new",
            {"user": pages.getAcessingUser(), "module": name},
        )

        modules_state.recalcModuleHashes()


def rmModule(module: str, message: str = "deleted") -> None:
    with modulesLock:
        x = modules_state.ActiveModules.pop(module)
        j = {
            i: copy.deepcopy(x[i])
            for i in x
            if not (isinstance(x[i], weakref.ref))
        }
        scopes.pop(module)

    for i in resource_types:
        resource_types[i].on_delete_module(module)

    # Delete any custom resource types hanging around.
    for k in j:
        if j[k].get("resource", {}).get("type") in resource_types:
            try:
                rt = j[k]["resource"]["type"]
                assert isinstance(rt, str)
                resource_types[rt].on_unload(module, k, j[k])
            except Exception:
                logging.exception("Error deleting resource type")
                messagebus.post_message(
                    "/system/notifications/errors",
                    f"Error deleting resource: {module},{k}",
                )

    # Get rid of any permissions defined in the modules.
    auth.importPermissionsFromModules()

    with modulesLock:
        if module in external_module_locations:
            del external_module_locations[module]

    fn = getModuleFn(module)

    if module not in external_module_locations:
        if os.path.exists(fn):
            shutil.rmtree(fn)

    modules_state.recalcModuleHashes()
    # Get rid of any garbage cycles associated with the event.
    gc.collect()
    messagebus.post_message("/system/modules/unloaded", module)
    messagebus.post_message(
        "/system/modules/deleted", {"user": pages.getAcessingUser()}
    )


def createResource(module: str, resource: str, data: ResourceDictType):
    """Insert a  resource and instantiate whatever it makes"""
    if resource in modules_state.ActiveModules[module]:
        raise RuntimeError(f"Resource already exists: {module}/{resource}")

    data = modules_state.mutable_copy_resource(data)
    data = modules_state.normalize_resource_data(data)
    modules_state.set_resource_error(module, resource, None)
    modules_state.raw_insert_resource(module, resource, data)
    handleResourceChange(module, resource, data, newly_added=True)


def handleResourceChange(
    module: str,
    resource: str,
    obj: ResourceDictType | None = None,
    newly_added: bool = False,
) -> None:
    with modules_state.modulesLock:
        t = modules_state.ActiveModules[module][resource]["resource"]["type"]

        resourceobj = modules_state.ActiveModules[module][resource]
        modules_state.set_resource_error(module, resource, None)

        assert isinstance(t, str)

        if resource == "__metadata__":
            return

        if not resourceobj["resource"].get("enable", True):
            return

        if t == "permission":
            auth.importPermissionsFromModules()
        if t == "module-description":
            pass
        else:
            if t not in resource_types:
                logger.warning(
                    f"Unknown resource type {t} for {resource}:{module}"
                )
            else:
                if not newly_added:
                    resource_types[t].on_update(module, resource, resourceobj)
                else:
                    resource_types[t].on_load(module, resource, resourceobj)


def enable_resource(module: str, resource: str):
    r = modules_state.ActiveModules[module][resource]
    rt = r["resource"]["type"]
    resource_types[rt].on_load(module, resource, r)
