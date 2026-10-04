# mypy: import-untyped=False
# pyright: reportIncompatibleMethodOverride=false
from __future__ import annotations

import copy
import gc
import json
import os
import shutil
import textwrap
import threading
import time
import traceback
import warnings
import weakref
from collections.abc import Callable, Mapping
from typing import Any

import iot_devices.device
import iot_devices.host
import pydantic
import quart
import structlog

# SPDX-License-Identifier: GPL-3.0-or-later
from kaithem.api import lifespan

from . import (
    alerts,
    directories,
    geolocation,
    messagebus,
    modules_state,
    pages,
    tagpoints,
    unitsofmeasure,
    widgets,
    workers,
)
from .modules_state import ResourceType, resource_types

SUBDEVICE_SEPARATOR = "/"

# Our lock to be the same lock as the modules lock otherwise
#  there would be too
# many easy ways to make a deadlock, we have to be able to
# edit the state because self modifying devices
#  exist and can be saved in a module
logger = structlog.get_logger(__name__)

saveLocation = os.path.join(directories.vardir, "devices")


# Used by device tag j2 template
callable = callable

device_types: dict[str, dict[str, Any]] = {}


def scan_devices():
    x = iot_devices.host.discover()
    device_types.clear()
    device_types.update(x)
    return x


workers.do(scan_devices)


def delete_bookkeep(name, confdir=False):
    # It sometimes is not there if the parent device got deleted first
    device_data_cache.pop(name, None)

    if name in devices_host.get_devices():
        rtd = devices_host.devices[name]
        pm = rtd.module_name
        pr = rtd.resource_name
        devices_host.close_device(name)
        gc.collect()

        if confdir:
            try:
                old_dev_conf_folder = get_config_folder_from_info(
                    pm, pr, name, create=False, always_return=True
                )
                if old_dev_conf_folder and os.path.isdir(old_dev_conf_folder):
                    if not old_dev_conf_folder.count("/") > 3:
                        # Basically since rmtree is so dangerous we make sure
                        # it absolutely cannot be any root or nearly root level
                        # folder
                        # in the user's home dir even if som
                        # unknown future error happens.
                        # I have no reason to think this will ever
                        # actually be needed.
                        raise RuntimeError(  # noqa: TRY301
                            f"Defensive check failed: {old_dev_conf_folder}"
                        )

                    shutil.rmtree(old_dev_conf_folder)
            except Exception:
                logger.exception("Err deleting conf dir")

        # Gotta be aggressive about ref cycle breaking!
        gc.collect()
        gc.collect()
        gc.collect()

        messagebus.post_message("/devices/removed/", name)


dbgd: weakref.WeakValueDictionary[str, DeviceRuntimeState] = (
    weakref.WeakValueDictionary()
)


finished_reading_resources = False
deffered_loaders_list_lock = threading.Lock()
# Add module and resource for deterministic sortability
deferred_loaders: list[tuple[str, str, Callable[[], None]]] = []


# module, resource, config
device_data_cache: dict[str, tuple[str, str, Mapping[str, Any]]] = {}


class DeviceResourceType(ResourceType):
    def on_finished_loading(self, module):
        if module is None:
            init_devices()
            global finished_reading_resources
            finished_reading_resources = True

    def on_load(self, module, resource, data):
        cls = None

        dev_data = data["device"]
        assert isinstance(dev_data, dict)

        # We may want to store a device in a shortened resource name
        # because of / issues.
        if "name" in dev_data:
            devname = dev_data["name"]
            assert isinstance(devname, str)
        else:
            raise ValueError("No name in device")

        device_data_cache[devname] = (module, resource, data["device"])

        if dev_data.get("is_subdevice", False):
            return

        def load_closure():
            try:
                old_dev = devices_host.devices.get(devname)
            except Exception:
                old_dev = None
            if old_dev and old_dev.device:
                m, r = old_dev.module_name, old_dev.resource_name

                if not (m, r) == (module, resource):
                    modules_state.set_resource_error(
                        module,
                        resource,
                        f"Device at {m}/{r} shares name",
                    )
                    raise RuntimeError(
                        "Can't overwrite device from a different resource"
                    )
                if old_dev.device.device_type.lower() not in (
                    "unsupported",
                    "unknown",
                    "unusedsubdevice",
                ):
                    if not old_dev.device.device_type == dev_data["type"]:
                        raise RuntimeError(
                            f"Expected {old_dev.device.device_type}"
                        )

            if devname in devices_host.devices:
                devices_host.close_device(devname)

            makeDevice(devname, dev_data, cls, module, resource)

        # We aren't finished loading all the modules at startup
        # Save it and do everything at once
        if finished_reading_resources:
            load_closure()
        else:
            with deffered_loaders_list_lock:
                deferred_loaders.append((module, resource, load_closure))

    def on_update(self, module, resource: str, data: Mapping[str, Any]):
        # TODO is this ugly? I think it will be called
        # whenever we change something
        # and only the change detection stops it from double loading
        if data["name"] in device_data_cache:
            if data["device"] == device_data_cache[data["name"]][2]:
                return

        self.on_load(module, resource, data)

    def on_unload(self, module, resource, data):
        with modules_state.modulesLock:
            n = resource.split(SUBDEVICE_SEPARATOR)[-1]
            if "name" in data["device"]:
                n = data["device"]["name"]

            delete_bookkeep(n, True)

    def on_create_request(self, module, resource, kwargs):
        raise RuntimeError("Not implemented, devices uses it's own create page")

    def create_page(self, module, path):
        return pages.get_template("devices/deviceintomodule.html").render(
            module=module, path=path
        )

    def edit_page(self, module: str, resource: str, data):
        name = modules_state.ActiveModules[module][resource]["device"]["name"]
        return quart.redirect("/device/" + name + "/manage")


drt = DeviceResourceType("device", mdi_icon="chip")
resource_types["device"] = drt


def getZombies():
    x = []
    try:
        v = devices_host.get_devices()
        for i in dbgd:
            if dbgd[i] not in v:
                x.append((i, dbgd[i]))
    except Exception:
        print(traceback.format_exc())
    return x


def get_config_folder_from_device(runtime_obj: DeviceRuntimeState, create=True):
    assert runtime_obj

    if not hasattr(runtime_obj, "parent_module") or not runtime_obj.module_name:
        module = None
        resource = None
    else:
        module = runtime_obj.module_name
        resource = runtime_obj.module_name

    return get_config_folder_from_info(
        module, resource, runtime_obj.name, create=create
    )


def get_config_folder_from_info(
    module: str | None,
    resource: str | None,
    name: str,
    create=True,
    always_return=False,
):
    if not module:
        saveLocation = os.path.join(
            directories.vardir, "devices", f"{name}.config.d"
        )
    else:
        # or '' makes linker happy, idk why it doesn't detect the if statement.
        saveLocation = os.path.join(
            directories.vardir,
            "modules",
            "data",
            module or "",
            "__filedata__",
            f"{resource or name}.config.d",
        )

    if not os.path.exists(saveLocation):
        if not create:
            if not always_return:
                return None
        else:
            os.makedirs(saveLocation, mode=0o700, exist_ok=True)

    return saveLocation


def makeBackgroundPrintFunction(p, t, title, self):
    def f():
        self.logWindow.write(f"<b>{title} at {t}</b><br>{p}")

    return f


def makeBackgroundErrorFunction(t, time, self):
    # Don't block everything up
    def f():
        self.logWindow.write(
            f'<div class="danger"><b>Error at {time}</b><br><pre>{t}</pre></div>'  # noqa: E501
        )

    return f


class DeviceRuntimeState(iot_devices.host.DeviceHostContainer):
    @staticmethod
    def makeGenericUIMsgHandler(wr):
        def f(u, v):
            wr().onGenericUIMessage(u, v)

        return f

    def __init__(
        self,
        host,
        parent: DeviceRuntimeState | None,
        config,
        module: str | None = None,
        resource: str | None = None,
    ):
        super().__init__(host, parent, config)

        self.module_name: str | None = module
        self.resource_name: str | None = resource

        if not module:
            if self.parent:
                self.module_name = self.parent.module_name

        if not resource:
            if self.parent:
                if self.parent.resource_name:
                    resource_dir = "/".join(
                        self.parent.resource_name.split("/")[:-1]
                    )
                    rel_name = ".d/".join(config["name"].split("/"))

                    if resource_dir:
                        self.resource_name = f"{resource_dir}/{rel_name}"
                    else:
                        self.resource_name = rel_name

        if self.name in device_data_cache:
            self.module, self.resource, _config = device_data_cache[self.name]
        # Which points to show in overview
        self.dashboard_datapoints = {}

        self.logWindow = widgets.ScrollingWindow(2500)

        self._tagBookKeepers: dict[str, Callable[[Any, float, Any], None]] = {}

        # This is for extra non device specific stuff we add to all devices
        self._generic_ws_channel = widgets.APIWidget()
        self._generic_ws_channel.require("system_admin")

        onMessage2 = self.makeGenericUIMsgHandler(weakref.ref(self))

        # I don't think this is actually needed
        self._uiMsgRef = onMessage2

        self._generic_ws_channel.attach(onMessage2)

        dbgd[config["name"] + str(time.time())] = self

        # Time, title, text tuples for any "messages" a device might "print"
        self.messages: list[tuple[float, str, str]] = []

        # This dict cannot be changed, only replaced atomically.
        # It is a list of alert objects. Dict keys
        # may not include special chars besides underscores.

        # It is a list of all alerts "owned" by the device.
        self.alerts: dict[str, alerts.Alert] = {}

        # A list of all the tag points owned by the device
        self.tagpoints: dict[str, tagpoints.GenericTagPointClass[Any]] = {}

        # Same dict keys as tagpoints, but a list of handlers
        self.tagpointhandlerfunctions: dict[
            str, Callable[[Any, float, Any], None]
        ] = {}

        # The new devices spec has
        # a way more limited idea of what
        # a data point is.
        self.datapoints: dict[
            str, int | float | str | bytes | Mapping[str, Any] | None
        ] = {}

        self.datapoint_timestamps: dict[str, float] = {}
        # Time, msg
        self.errors: list[tuple[float, str]] = []

        self.getters: dict[
            str, Callable[[iot_devices.device.DataRequest], Any] | None
        ] = {}

    def on_device_ready(self, device: iot_devices.device.Device):
        super().on_device_ready(device)

        data: dict[str, Any] = dict(copy.deepcopy(device.config))

        if "extensions" not in data:
            data["extensions"] = {}

        if "kaithem" not in data["extensions"]:
            data["extensions"]["kaithem"] = {}

        # Legacy porting block
        # TODO
        if "kaithem.use_default_alerts" in data:
            warnings.warn("Auto upgrading legacy config key")
            data["extensions"]["kaithem"]["use_default_alerts"] = data.pop(
                "kaithem.use_default_alerts"
            )
        if "kaithem.read_perms" in data:
            warnings.warn("Auto upgrading legacy config key")
            data["extensions"]["kaithem"]["read_perms"] = data.pop(
                "kaithem.read_perms"
            )
        if "kaithem.write_perms" in data:
            warnings.warn("Auto upgrading legacy config key")
            data["extensions"]["kaithem"]["write_perms"] = data.pop(
                "kaithem.write_perms"
            )

        if device.device_type not in (
            "unsupported",
            "unknown",
            "UnusedSubDevice",
        ):
            device.update_config(data)

        # Legacy stuff used strings
        self.k_use_default_alerts = str(
            data.get("extensions", {})
            .get("kaithem", {})
            .get("use_default_alerts")
        )

    def on_after_device_removed(self):
        for i in self.tagpointhandlerfunctions:
            f = self.tagpointhandlerfunctions[i]
            try:
                self.tagpoints[i].unsubscribe(f)
            except Exception:
                logger.exception(f"Error unsubscribing from tagpoint {i}")

        self.tagpointhandlerfunctions.clear()

        try:
            for i in self._tagBookKeepers:
                if i in self.tagpoints:
                    self.tagpoints[i].unsubscribe(self._tagBookKeepers[i])
        except Exception:
            if not lifespan.is_shutting_down:
                logger.exception(
                    "unsub fail from tagpoints while closing device"
                )

        self._tagBookKeepers.clear()

        try:
            self.tagpoints.clear()
        except Exception:
            pass

        try:
            for i in self.alerts:
                try:
                    self.alerts[i].release()
                except Exception:
                    logger.exception("Error releasing alerts")

                try:
                    self.alerts[i].close()
                except Exception:
                    logger.exception("Error cleaning up alerts")

        except Exception:
            logger.exception("Error releasing alerts")

    def onGenericUIMessage(self, u, v):
        if v[0] == "set":
            if v[2] is not None:
                self.tagpoints[v[1]].value = v[2]

        if v[0] == "fake":
            if v[2] is not None:
                self.tagpoints[v[1]]._k_ui_fake = self.tagpoints[v[1]].claim(
                    v[2], "faked", priority=50.5
                )

            else:
                if hasattr(self.tagpoints[v[1]], "_k_ui_fake"):
                    self.tagpoints[v[1]]._k_ui_fake.release()

        elif v[0] == "refresh":
            self.tagpoints[v[1]].get_vta()

    def on_data_change(self, name: str, value, timestamp: float, annotation):
        """used for subclassing, this is how you watch for data changes.
        Kaithem does not need this, we have direct observable tag points.
        """

    def get_full_schema(self):
        assert self.device
        s = self.device.get_full_schema()
        if "extensions" not in s["properties"]:
            s["properties"]["extensions"] = {}
        s["properties"]["extensions"]["kaithem"] = {}

        s["properties"]["extensions"]["kaithem"]["read_perms"] = {
            "type": "string",
            "title": "Read Permissions",
            "description": "Comma separated permissions required to read",
        }

        s["properties"]["extensions"]["kaithem"]["write_perms"] = {
            "type": "string",
            "title": "Write Permissions",
            "description": "Comma separated permissions required to write",
        }

        s["properties"]["extensions"]["kaithem"]["use_default_alerts"] = {
            "type": "boolean",
            "title": "Enable the device's default alerts",
            "default": True,
        }

        return s


class DevicesHost(iot_devices.host.Host[DeviceRuntimeState]):
    def on_device_print(self, device, message, title=""):
        "Print a message to the Device's management page"
        t = textwrap.fill(str(message), 120)
        tm = unitsofmeasure.strftime(time.time())

        if title:
            t = f"<b>{title}</b><br>{t}"
        t = t.replace("\n", "<br>")

        # Can't use a def here, wouldn't want it to possibly
        # capture more than just a string,
        # And keep stuff from GCIng for too long
        workers.do(makeBackgroundPrintFunction(t, tm, title, device))

    def get_config_folder(self, device: DeviceRuntimeState, create=True):
        return get_config_folder_from_device(device, create=create)

    def __setupTagPerms(self, device: DeviceRuntimeState, t, writable=True):
        # Devices can have a default exposure

        our_ext = device.config.get("extensions", {}).get("kaithem", {})

        read_perms = (
            our_ext.get("read_perms", "system_admin").strip() or "system_admin"
        )
        write_perms = (
            our_ext.get("write_perms", "system_admin").strip() or "system_admin"
        )
        t.expose(read_perms, write_perms if writable else [])

    def resolve_datapoint_name(self, device_name, datapoint_name):
        return f"/devices/{device_name}.{datapoint_name}"

    def numeric_data_point(
        self,
        device: str,
        name: str,
        min: float | None = None,
        max: float | None = None,
        hi: float | None = None,
        lo: float | None = None,
        default: float | None = None,
        description: str = "",
        unit: str = "",
        handler: Callable[[float, float, Any], Any] | None = None,
        interval: float = 0,
        writable: bool = True,
        subtype: str = "",
        dashboard: bool = True,
        on_request: Callable[[iot_devices.device.DataRequest], Any]
        | None = None,
        **kwargs,
    ):
        tagname = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]

        t = tagpoints.Tag(tagname)

        self.__setupTagPerms(runtimedata, t, writable)

        t.min = min
        t.max = max
        t.hi = hi
        t.lo = lo
        t.description = description
        t.unit = unit
        t.default = default or 0
        t.interval = interval
        t.subtype = subtype
        t.writable = writable

        runtimedata.dashboard_datapoints[tagname] = dashboard

        # Be defensive
        if tagname in runtimedata.tagpointhandlerfunctions:
            t.unsubscribe(runtimedata.tagpointhandlerfunctions[tagname])

        if handler:
            runtimedata.tagpointhandlerfunctions[tagname] = handler
            t.subscribe(handler)

        runtimedata.tagpoints[tagname] = t

        def req(c: tagpoints.Claim):
            d = iot_devices.device.DataRequest()
            if on_request:
                on_request(d)

        t.default_claim.getter = req

        messagebus.post_message("/system/tags/configured", t.name)

    def string_data_point(
        self,
        device: str,
        name: str,
        description: str = "",
        handler: Callable[[str, float, Any], Any] | None = None,
        default: str | None = None,
        interval: float = 0,
        writable: bool = True,
        subtype: str = "",
        dashboard: bool = True,
        on_request: Callable[[iot_devices.device.DataRequest], Any]
        | None = None,
        **kwargs,
    ):
        tagname = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]

        t = tagpoints.StringTag(tagname)

        self.__setupTagPerms(runtimedata, t, writable)

        t.default = default or ""
        t.interval = interval
        t.subtype = subtype
        t.writable = writable

        runtimedata.dashboard_datapoints[tagname] = dashboard

        # Be defensive
        if tagname in runtimedata.tagpointhandlerfunctions:
            t.unsubscribe(runtimedata.tagpointhandlerfunctions[name])

        if handler:
            runtimedata.tagpointhandlerfunctions[tagname] = handler
            t.subscribe(handler)

        runtimedata.tagpoints[tagname] = t

        def req(c: tagpoints.Claim):
            d = iot_devices.device.DataRequest()
            if on_request:
                on_request(d)

        t.default_claim.getter = req

        messagebus.post_message("/system/tags/configured", t.name)

    def object_data_point(
        self,
        device: str,
        name: str,
        description: str = "",
        handler: Callable[[dict[str, Any], float, Any], Any] | None = None,
        interval: float = 0,
        writable: bool = True,
        subtype: str = "",
        dashboard: bool = True,
        default: dict[str, Any] | None = None,
        on_request: Callable[[iot_devices.device.DataRequest], Any]
        | None = None,
        **kwargs,
    ):
        tagname = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]

        t = tagpoints.ObjectTag(tagname)

        self.__setupTagPerms(runtimedata, t, writable)

        t.default = default or {}
        t.interval = interval
        t.subtype = subtype
        t.writable = writable

        runtimedata.dashboard_datapoints[tagname] = dashboard

        # Be defensive
        if tagname in runtimedata.tagpointhandlerfunctions:
            t.unsubscribe(runtimedata.tagpointhandlerfunctions[tagname])

        if handler:
            runtimedata.tagpointhandlerfunctions[tagname] = handler
            t.subscribe(handler)

        runtimedata.tagpoints[tagname] = t

        def req(c: tagpoints.Claim):
            d = iot_devices.device.DataRequest()
            if on_request:
                on_request(d)

        t.default_claim.getter = req

        messagebus.post_message("/system/tags/configured", t.name)

    def bytestream_data_point(
        self,
        device: str,
        name: str,
        description: str = "",
        handler: Callable[[bytes, float, Any], Any] | None = None,
        interval: float = 0,
        writable: bool = True,
        subtype: str = "",
        dashboard: bool = True,
        default: bytes | None = None,
        on_request: Callable[[iot_devices.device.DataRequest], Any]
        | None = None,
        **kwargs,
    ):
        tagname = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]
        t = tagpoints.BinaryTag(tagname)

        self.__setupTagPerms(runtimedata, t, writable)

        t.default = default or b""
        t.interval = interval
        t.subtype = subtype
        t.writable = writable

        runtimedata.dashboard_datapoints[tagname] = dashboard

        # Be defensive
        if tagname in runtimedata.tagpointhandlerfunctions:
            t.unsubscribe(runtimedata.tagpointhandlerfunctions[tagname])

        if handler:
            runtimedata.tagpointhandlerfunctions[tagname] = handler
            t.subscribe(handler)

        runtimedata.tagpoints[tagname] = t

        def req(c: tagpoints.Claim):
            d = iot_devices.device.DataRequest()
            if on_request:
                on_request(d)

        t.default_claim.getter = req

        messagebus.post_message("/system/tags/configured", t.name)

    def fast_push_bytes(
        self,
        device: str,
        name: str,
        value: bytes,
        timestamp: float | None = None,
        annotation: Any | None = None,
        force_push_on_repeat: bool = False,
    ):
        n = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]
        runtimedata.tagpoints[n].fast_push(value, timestamp, annotation)

    def set_alarm(
        self,
        device: iot_devices.device.Device,
        name: str,
        datapoint: str,
        expression: str,
        priority: str = "info",
        trip_delay: float = 0,
        auto_ack: bool = False,
        release_condition: str | None = None,
        **kw,
    ):
        n = self.resolve_datapoint_name(device.name, datapoint)
        runtimedata = device.get_host_container()
        use_default_alerts = (
            runtimedata.config.get("extensions", {})
            .get("kaithem", {})
            .get("use_default_alerts", True)
        )
        t = tagpoints.allTags[n]()
        assert t
        x = t.set_alarm(
            name,
            condition=expression,
            priority=priority,
            trip_delay=trip_delay,
            auto_ack=auto_ack,
            release_condition=release_condition,
            enabled=use_default_alerts,
        )
        if x:
            runtimedata.alerts[name] = x
        else:
            raise RuntimeError("Alarm setter returned nothing")

    def set_data_point(
        self,
        device: str,
        name,
        value: str | int | float | str | bytes | Mapping[str, Any],
        timestamp=None,
        annotation=None,
        force_push_on_repeat: bool = False,
    ):
        n = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]
        runtimedata.tagpoints[n](value, timestamp, annotation)

    def set_number(
        self,
        device: str,
        name: str,
        value: float | int,
        timestamp: float | None = None,
        annotation: Any | None = None,
        force_push_on_repeat: bool = False,
    ):
        self.set_data_point(
            device, name, value, timestamp, annotation, force_push_on_repeat
        )

    def set_string(
        self,
        device: str,
        name: str,
        value: str,
        timestamp: float | None = None,
        annotation: Any | None = None,
        force_push_on_repeat: bool = False,
    ):
        self.set_data_point(
            device, name, value, timestamp, annotation, force_push_on_repeat
        )

    def set_bytes(
        self,
        device: str,
        name: str,
        value: bytes,
        timestamp: float | None = None,
        annotation: Any | None = None,
        force_push_on_repeat: bool = False,
    ):
        self.set_data_point(
            device, name, value, timestamp, annotation, force_push_on_repeat
        )

    def set_object(
        self,
        device: str,
        name: str,
        value: dict[str, Any],
        timestamp: float | None = None,
        annotation: Any | None = None,
        force_push_on_repeat: bool = False,
    ):
        self.set_data_point(
            device, name, value, timestamp, annotation, force_push_on_repeat
        )

    def get_datapoint(self, device: str, name: str):
        n = self.resolve_datapoint_name(device, name)
        runtimedata = devices_host.devices[device]
        return runtimedata.tagpoints[n].get_vta()

    def get_number(
        self, device: str, datapoint: str
    ) -> tuple[float | int, float, Any]:
        return self.get_datapoint(device, datapoint)

    def get_string(self, device: str, datapoint: str) -> tuple[str, float, Any]:
        return self.get_datapoint(device, datapoint)

    def get_bytes(
        self, device: str, datapoint: str
    ) -> tuple[bytes, float, Any]:
        return self.get_datapoint(device, datapoint)

    def get_object(
        self, device: str, datapoint: str
    ) -> tuple[dict[str, Any], float, Any]:
        return self.get_datapoint(device, datapoint)

    def get_config_for_device(
        self, parent_device: Any | None, full_device_name: str
    ) -> Mapping[str, Any]:
        return device_data_cache.pop(full_device_name, ("", "", {}))[2]

    def on_config_changed(
        self, device: DeviceRuntimeState, config: Mapping[str, Any]
    ):
        def f():
            with modules_state.modulesLock:
                if device.module_name and device.resource_name:
                    # Todo why are we mutating in place?
                    devdata = modules_state.ActiveModules[device.module_name][
                        device.resource_name
                    ]

                    devdata = dict(copy.deepcopy(devdata))
                    devdata["device"] = dict(copy.deepcopy(config))

                    m, r = device.module_name, device.resource_name

                    modules_state.raw_insert_resource(m, r, devdata)

            use_default_alerts = (
                config.get("extensions", {})
                .get("kaithem", {})
                .get("use_default_alerts", True)
            )

            if use_default_alerts != device.k_use_default_alerts:
                device.k_use_default_alerts = use_default_alerts

                if not use_default_alerts:
                    for alert in device.alerts.values():
                        alert.disable()

                else:
                    for alert in device.alerts.values():
                        alert.enable()

    def on_device_error(self, container: DeviceRuntimeState, s: str):
        container.errors.append((time.time(), str(s)))

        if container.errors:
            if time.time() > container.errors[-1][0] + 15:
                logger.error(f"in device: {container.name}\n{s}")
            else:
                logger.error(f"in device: {container.name}\n{s}")

        if len(container.errors) > 50:
            container.errors.pop(0)

        workers.do(
            makeBackgroundErrorFunction(
                textwrap.fill(s, 120),
                unitsofmeasure.strftime(time.time()),
                container,
            )
        )
        if len(container.errors) == 1:
            messagebus.post_message(
                "/system/notifications/errors",
                f"First error in device: {container.name}",
            )
            logger.error(f"in device: {container.name}\n{s}")


devices_host = DevicesHost(DeviceRuntimeState)
devices_host.host_apis["get_site_coordinates"] = (
    geolocation.deviceLocationGetter
)


class UnsupportedDevice(iot_devices.device.Device):
    description = (
        "This device does not have support, or else the support is not loaded."
    )
    device_type = "unsupported"

    def warn(self):
        self.handle_error("This device type has no support.")

    def __init__(self, data):
        super().__init__(data)


# Device data always has 2 constants. 1 is the required type, the other
# is name, and that's optional but can be used to rename a device


_PLACEHOLDER_DEVICE_TYPES = ("unsupported", "unknown", "unusedsubdevice")


def _safe_rmtree(path: str):
    """rmtree guarded by the same depth check used everywhere else."""
    if not path.count("/") > 3:
        # Basically since rmtree is so dangerous we make sure
        # it absolutely cannot be any root or nearly root level folder
        # in the user's home dir even if some unknown future error happens.
        raise RuntimeError(f"Defensive check failed: {path}")
    shutil.rmtree(path)


def _rename_or_replace(src: str, dst: str):
    """Rename within one filesystem, fall back to copy+delete across them."""
    try:
        os.rename(src, dst)
    except OSError:
        shutil.copytree(src, dst, dirs_exist_ok=True)
        _safe_rmtree(src)


def _default_child_resource(parent_resource: str, full_name: str) -> str:
    """Default resource for a subdevice, mirroring
    DeviceRuntimeState.__init__."""
    d = parent_resource.rsplit("/", 1)[0] if "/" in parent_resource else ""
    rel = ".d/".join(full_name.split("/"))
    return f"{d}/{rel}" if d else rel


def _default_child_prefix(parent_name: str, parent_resource: str) -> str:
    d = parent_resource.rsplit("/", 1)[0] if "/" in parent_resource else ""
    base = parent_name + ".d/"
    return f"{d}/{base}" if d else base


def _check_device_type(
    runtime_obj: DeviceRuntimeState, data: Mapping[str, Any]
):
    "Same type check the startup resource loader applies."
    dev = runtime_obj.device
    if dev is None:
        return
    if dev.device_type.lower() in _PLACEHOLDER_DEVICE_TYPES:
        return
    if dev.device_type != data["type"]:
        raise RuntimeError(f"Expected {dev.device_type}")


def _enumerate_device_tree(devname: str) -> dict[str, dict[str, Any]]:
    """The parent and every device whose name has the devname + '/' prefix.

    Persisted module resources are authoritative for where a device lives:
    a recreated subdevice's runtime container only carries the default
    (and not even storable) resource path. Live runtime devices are merged
    in so dynamic subdevices that have no saved resource are still seen.
    """
    out: dict[str, dict[str, Any]] = {}

    for mm, mod in modules_state.ActiveModules.items():
        for rr, cand in mod.items():
            if cand.get("resource", {}).get("type") != "device":
                continue
            n = cand.get("device", {}).get("name")
            if n == devname or (
                isinstance(n, str) and n.startswith(devname + "/")
            ):
                out[n] = {
                    "module": mm,
                    "resource": rr,
                    "config": copy.deepcopy(cand["device"]),
                    "resource_data": cand,
                }

    for n, cont in devices_host.get_devices().items():
        if (n == devname or n.startswith(devname + "/")) and n not in out:
            out[n] = {
                "module": cont.module_name,
                "resource": cont.resource_name,
                "config": copy.deepcopy(dict(cont.config)),
                "resource_data": None,
            }

    return out


def _map_child_location(
    old_parent: str,
    new_parent: str,
    old_parent_resource: str,
    new_parent_resource: str,
    new_module: str,
    old_child_resource: str | None,
    old_full: str,
    new_full: str,
) -> tuple[str, str]:
    """Where a subdevice's resource ends up after the parent moves."""
    old_cr = old_child_resource or _default_child_resource(
        old_parent_resource, old_full
    )

    new_default = _default_child_resource(new_parent_resource, new_full)
    old_prefix = _default_child_prefix(old_parent, old_parent_resource)
    new_prefix = _default_child_prefix(new_parent, new_parent_resource)

    if old_cr == _default_child_resource(old_parent_resource, old_full):
        return new_module, new_default
    if old_cr.startswith(old_prefix):
        return new_module, new_prefix + old_cr[len(old_prefix) :]

    # A custom resource location keeps its relative path; it follows the
    # parent into the target module on cross-module moves. Occupancy is
    # checked up front so a clash there aborts before the switch.
    return new_module, old_cr


def _make_switch_item(
    old_name: str,
    new_name: str,
    old_m: str | None,
    old_r: str | None,
    new_m: str,
    new_r: str,
    old_data: dict[str, Any] | None,
    device_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    if old_data is not None:
        new_data = copy.deepcopy(old_data)
        new_data["device"] = copy.deepcopy(dict(device_cfg))
    else:
        new_data = {
            "resource": {"type": "device", "modified": int(time.time())},
            "device": copy.deepcopy(dict(device_cfg)),
        }
    return {
        "old_name": old_name,
        "new_name": new_name,
        "old_m": old_m,
        "old_r": old_r,
        "new_m": new_m,
        "new_r": new_r,
        "old_data": old_data,
        "new_data": new_data,
    }


def _validate_switch_items(
    items: list[dict[str, Any]], old_tree_names: set[str]
):
    """Upfront same-name / occupancy / path conflict checks for the whole
    tree, so no conflict can surface halfway through the switch."""
    seen_locs: set[tuple[str, str]] = set()

    for it in items:
        nn = it["new_name"]
        if nn in devices_host.devices and nn not in old_tree_names:
            raise RuntimeError(f"Device name already exists: {nn}")

        if nn in device_data_cache:
            cm, cr, _ = device_data_cache[nn]
            # A lingering cache entry at the device's own old location is
            # expected (it gets swapped in the same transaction); anything
            # pointing elsewhere is a real same-name conflict.
            if (cm, cr) != (it["new_m"], it["new_r"]) and not (
                nn in old_tree_names
                and (cm, cr) == (it["old_m"], it["old_r"])
            ):
                raise RuntimeError(
                    f"Can't overwrite device from a different resource: {nn}"
                )

        loc = (it["new_m"], it["new_r"])
        if loc in seen_locs:
            raise RuntimeError(
                f"Two devices would be saved to the same resource: {loc}"
            )
        seen_locs.add(loc)

        am = modules_state.ActiveModules[it["new_m"]]
        if it["new_r"] in am:
            if am[it["new_r"]]["resource"]["type"] != "device":
                raise ValueError(
                    "Nondevice obj in the module with that name exists."
                )
            stored_name = am[it["new_r"]].get("device", {}).get("name")
            if stored_name not in (it["old_name"], nn):
                raise RuntimeError(
                    f"Resource already holds another device: {loc}"
                )

        # Same name rules raw_insert_resource enforces, checked early so
        # an unwritable target can't abort the switch mid-commit.
        modules_state.check_forbidden(it["new_r"])
        ensure_module_path_ok(it["new_m"], it["new_r"])


class _DeviceTreeSwitch:
    """Stages, commits and rolls back moving one device tree (parent plus
    its configured subdevices) between module resources, including their
    __filedata__ config folders.

    prepare_*() must run while the old device is still live. commit_*()
    performs the one-way switch; on any failure rollback_*() restores the
    exact old folders and module resources (memory and disk).
    """

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self._token = f"{os.getpid()}_{time.time_ns()}"

        self.folder_ops: list[dict[str, Any]] = []
        self.prewritten: list[tuple[str, str]] = []

        # Resource journal:
        self.inserted: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str, dict[str, Any]]] = []
        self.created_dirs: set[tuple[str, str]] = set()
        self.removed_dirs: list[tuple[str, str, dict[str, Any]]] = []

    # ----- config folders -----

    def prepare_folders(self):
        for it in self.items:
            old_m, old_r = it["old_m"], it["old_r"]
            if not old_m or not old_r:
                continue
            old_folder = get_config_folder_from_info(
                old_m,
                old_r,
                it["old_name"],
                create=False,
                always_return=True,
            )
            new_folder = get_config_folder_from_info(
                it["new_m"],
                it["new_r"],
                it["new_name"],
                create=False,
                always_return=True,
            )
            if not old_folder or old_folder == new_folder:
                continue

            op: dict[str, Any] = {
                "old": old_folder,
                "new": new_folder,
                "staging": f"{new_folder}.staging-{self._token}",
                "bak": f"{old_folder}.bak-{self._token}",
                "aside": None,
                "copied": False,
                "preexisting_new": os.path.exists(new_folder),
            }
            if os.path.exists(old_folder):
                if os.path.exists(op["staging"]):
                    _safe_rmtree(op["staging"])
                # The old device stays online: this is only a copy.
                shutil.copytree(old_folder, op["staging"])
                op["copied"] = True
            self.folder_ops.append(op)

    def abort_prepare(self):
        for op in self.folder_ops:
            if os.path.exists(op["staging"]):
                shutil.rmtree(op["staging"], ignore_errors=True)
        for mm, rr in self.prewritten:
            try:
                self._remove_resource_files(mm, rr)
            except Exception:
                logger.exception("Err cleaning up prewritten resource")
        self.folder_ops.clear()
        self.prewritten.clear()

    def commit_folders(self):
        for op in self.folder_ops:
            if not op["copied"]:
                continue
            if op["preexisting_new"] and os.path.exists(op["new"]):
                op["aside"] = f"{op['new']}.aside-{self._token}"
                os.rename(op["new"], op["aside"])
            if os.path.exists(op["old"]):
                _rename_or_replace(op["old"], op["bak"])
            os.rename(op["staging"], op["new"])

    def rollback_folders(self):
        for op in reversed(self.folder_ops):
            if not op["copied"]:
                continue
            try:
                if os.path.exists(op["new"]):
                    _safe_rmtree(op["new"])
                if op["aside"] and os.path.exists(op["aside"]):
                    os.rename(op["aside"], op["new"])
                if os.path.exists(op["bak"]):
                    _rename_or_replace(op["bak"], op["old"])
            except Exception:
                logger.exception("Err restoring config folder")
            finally:
                for p in (op["staging"], op["aside"], op["bak"]):
                    if p and os.path.exists(p):
                        shutil.rmtree(p, ignore_errors=True)

    def cleanup_folders(self):
        for op in self.folder_ops:
            for p in (op["staging"], op["aside"], op["bak"]):
                if p and os.path.exists(p):
                    shutil.rmtree(p, ignore_errors=True)
        self.folder_ops.clear()

    # ----- module resources -----

    @staticmethod
    def _remove_resource_files(module: str, resource: str):
        "Remove a resource's files and any directories left empty."
        d = modules_state.get_resource_save_location(module, resource)
        base = resource.rsplit("/", 1)[-1]
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.startswith(base + "."):
                    os.remove(os.path.join(d, f))

        parts = resource.split("/")[:-1]
        moddir = modules_state.getModuleDir(module)
        while parts:
            try:
                os.rmdir(os.path.join(moddir, *parts))
            except OSError:
                break
            parts.pop()

    def prepare_writes(self):
        """Pre-write files for resources that move to a new location, so
        a disk failure is raised while the old device still runs."""
        try:
            for it in self.items:
                oldloc = (it["old_m"], it["old_r"])
                newloc = (it["new_m"], it["new_r"])
                if oldloc == newloc:
                    continue
                modules_state.save_resource(
                    it["new_m"], it["new_r"], it["new_data"]
                )
                self.prewritten.append(newloc)
        except Exception:
            self.abort_prepare()
            raise

    def _prune_old_parents(self, module: str, resource: str):
        parts = resource.split("/")[:-1]
        mod = modules_state.ActiveModules[module]
        while parts:
            d = "/".join(parts)
            if d in mod and mod[d]["resource"]["type"] == "directory":
                if any(k != d and k.startswith(d + "/") for k in mod):
                    break
                self.removed_dirs.append(
                    (module, d, copy.deepcopy(mod[d]))
                )
                del mod[d]
                try:
                    os.rmdir(
                        os.path.join(
                            modules_state.getModuleDir(module), *parts
                        )
                    )
                except OSError:
                    pass
            else:
                break
            parts.pop()

    def commit_resources(self):
        try:
            # New locations first, old locations afterwards, so a failure
            # in between still leaves the original data recoverable.
            for it in self.items:
                oldloc = (it["old_m"], it["old_r"])
                newloc = (it["new_m"], it["new_r"])
                if oldloc == newloc:
                    if it["old_data"] is not None:
                        self.removed.append(
                            (
                                oldloc[0],
                                oldloc[1],
                                copy.deepcopy(it["old_data"]),
                            )
                        )
                    modules_state.raw_insert_resource(
                        it["new_m"], it["new_r"], it["new_data"]
                    )
                else:
                    before = set(
                        modules_state.ActiveModules[it["new_m"]]
                    )
                    modules_state.raw_insert_resource(
                        it["new_m"], it["new_r"], it["new_data"]
                    )
                    for d in (
                        set(modules_state.ActiveModules[it["new_m"]])
                        - before
                    ):
                        if (
                            modules_state.ActiveModules[it["new_m"]][d][
                                "resource"
                            ]["type"]
                            == "directory"
                        ):
                            self.created_dirs.add((it["new_m"], d))
                    self.inserted.append(newloc)

            for it in self.items:
                oldloc = (it["old_m"], it["old_r"])
                newloc = (it["new_m"], it["new_r"])
                if oldloc == newloc or it["old_data"] is None:
                    continue
                mm, rr = oldloc
                assert mm and rr
                if (
                    mm in modules_state.ActiveModules
                    and rr in modules_state.ActiveModules[mm]
                ):
                    self.removed.append(
                        (mm, rr, copy.deepcopy(it["old_data"]))
                    )
                    modules_state.rawDeleteResource(mm, rr)
                self._prune_old_parents(mm, rr)
        except Exception:
            self.rollback_resources()
            raise

    def rollback_resources(self):
        # Undo inserts at brand new locations (memory + prewritten files).
        for mm, rr in reversed(self.inserted):
            try:
                if (
                    mm in modules_state.ActiveModules
                    and rr in modules_state.ActiveModules[mm]
                ):
                    modules_state.rawDeleteResource(mm, rr)
            except Exception:
                logger.exception("Err removing switched resource")

        for mm, d in sorted(
            self.created_dirs, key=lambda x: -x[1].count("/")
        ):
            try:
                mod = modules_state.ActiveModules.get(mm, {})
                if (
                    d in mod
                    and mod[d]["resource"]["type"] == "directory"
                    and not any(
                        k != d and k.startswith(d + "/") for k in mod
                    )
                ):
                    del mod[d]
                    os.rmdir(
                        os.path.join(
                            modules_state.getModuleDir(mm),
                            *d.split("/"),
                        )
                    )
            except OSError:
                pass
            except Exception:
                logger.exception("Err pruning rolled back dir resource")

        # Any prewritten file whose insert never happened, or that the
        # delete above did not cover, must also disappear.
        for it in self.items:
            oldloc = (it["old_m"], it["old_r"])
            newloc = (it["new_m"], it["new_r"])
            if oldloc == newloc:
                continue
            am = modules_state.ActiveModules.get(it["new_m"], {})
            if it["new_r"] not in am:
                try:
                    self._remove_resource_files(it["new_m"], it["new_r"])
                except Exception:
                    logger.exception("Err removing prewritten resource")

        # Restore directories before the device resources that live in them.
        for mm, d, data in sorted(
            self.removed_dirs, key=lambda x: x[1].count("/")
        ):
            try:
                modules_state.raw_insert_resource(mm, d, data)
            except Exception:
                logger.exception("Err restoring directory resource")

        # Put the original resource data back (also rewrites the disk file).
        for mm, rr, data in reversed(self.removed):
            try:
                modules_state.raw_insert_resource(mm, rr, data)
            except Exception:
                logger.exception("Err restoring original resource")

        self.inserted.clear()
        self.removed.clear()
        self.created_dirs.clear()
        self.removed_dirs.clear()


def updateDevice(
    devname: str, kwargs: dict[str, Any], saveChanges: bool = True
):
    with modules_state.modulesLock:
        m = kwargs["temp.kaithem.store_in_module"]
        r = kwargs["temp.kaithem.store_in_resource"]
        kwargs.pop("temp.kaithem.store_in_module", None)
        kwargs.pop("temp.kaithem.store_in_resource", None)

        # Stage 1, step 1: the JSON must parse before anything is touched.
        # The edit form carries a rename inside the JSON itself, so the
        # new name comes from there; a top level kwarg name is accepted
        # from API callers but the two may not disagree.
        decoded = json.loads(kwargs["json"])

        json_name = decoded.get("name")
        kwarg_name = kwargs.get("name")

        if kwarg_name and json_name and kwarg_name != json_name:
            raise ValueError("Internal error: device data inconsistent")

        # The NEW name, which could just be the old name
        name = json_name or kwarg_name or devname
        kwargs["name"] = name

        savable_data = {
            i: decoded[i]
            for i in decoded
            if (
                (not i.startswith("temp."))
                and not i.startswith("filedata.")
            )
        }

        if "name" not in savable_data:
            # We might update this from some odd place that doesn't
            # explicitly put name in data.
            savable_data["name"] = name

        if "type" not in savable_data:
            raise ValueError("Device config is missing a type")

        # Existing target side checks, unchanged.
        if m:
            if m not in modules_state.ActiveModules:
                raise ValueError("Can't store in nonexistant module")

            if r in modules_state.ActiveModules[m]:
                if (
                    not modules_state.ActiveModules[m][r]["resource"][
                        "type"
                    ]
                    == "device"
                ):
                    raise ValueError(
                        "Nondevice obj in the module with that name exists."
                    )

                if "module_lock" in modules_state.get_module_metadata(m):
                    raise PermissionError("Module is locked")

                if "lock" in modules_state.ActiveModules[m][r]["resource"]:
                    raise PermissionError("Device is locked")
            # Make sure we don't corrupt state by putting a folder
            # where a file already is
            ensure_module_path_ok(m, r)
        else:
            raise RuntimeError(
                "You can now only save devices into modules."
            )

        if devname not in devices_host.devices:
            raise RuntimeError("No such device to update")

        current_device_object = devices_host.devices[devname]

        subdevice = current_device_object.parent is not None

        parent_module = current_device_object.module_name
        parent_resource = current_device_object.resource_name

        if subdevice:
            return _update_subdevice_in_place(
                devname,
                current_device_object,
                savable_data,
                m,
                r,
            )

        assert (parent_module and parent_resource) or (
            not parent_module and not parent_resource
        )

        if not parent_module or not parent_resource:
            raise ValueError("Must choose module")

        _check_device_type(current_device_object, savable_data)

        if (
            parent_module in modules_state.ActiveModules
            and parent_resource
            in modules_state.ActiveModules[parent_module]
        ):
            dt = modules_state.ActiveModules[parent_module][parent_resource][
                "device"
            ]
        else:
            dt = {}

        assert isinstance(dt, dict)

        # Not the same as currently being a subdevice.
        # We have placeholders to edit subdevices that don't exist.
        configuredAsSubdevice = dt.get("is_subdevice", False)

        configuredAsSubdevice = (
            configuredAsSubdevice or dt.get("parent_device", "").strip()
        )  # type: ignore

        if configuredAsSubdevice:
            savable_data["is_subdevice"] = True

        newparent_module = m
        newparent_resource = r or name

        tree = _enumerate_device_tree(devname)
        old_parent_rd = tree[devname]["resource_data"]

        items: list[dict[str, Any]] = [
            _make_switch_item(
                devname,
                name,
                parent_module,
                parent_resource,
                newparent_module,
                newparent_resource,
                old_parent_rd,
                savable_data,
            )
        ]
        old_tree_names = {devname}

        for child_name in sorted(k for k in tree if k != devname):
            info = tree[child_name]
            # Runtime-only dynamic subdevices have no saved resource;
            # the plugin recreates (or not) those on its own.
            if info["resource_data"] is None:
                continue
            suffix = child_name[len(devname) + 1 :]
            new_child_name = f"{name}/{suffix}"
            new_child_m, new_child_r = _map_child_location(
                devname,
                name,
                parent_resource,
                newparent_resource,
                newparent_module,
                info["resource"],
                child_name,
                new_child_name,
            )
            child_cfg = copy.deepcopy(info["config"])
            child_cfg["name"] = new_child_name
            child_cfg["is_subdevice"] = True
            items.append(
                _make_switch_item(
                    child_name,
                    new_child_name,
                    info["module"],
                    info["resource"],
                    new_child_m,
                    new_child_r,
                    info["resource_data"],
                    child_cfg,
                )
            )
            old_tree_names.add(child_name)

        _validate_switch_items(items, old_tree_names)

        switch = _DeviceTreeSwitch(items)

        # Stage everything that can be staged while the old device runs.
        try:
            switch.prepare_folders()
            switch.prepare_writes()
        except Exception:
            switch.abort_prepare()
            raise

        initial_visible = set(devices_host.get_devices())
        old_cache = {
            n: copy.deepcopy(device_data_cache[n])
            for n in old_tree_names
            if n in device_data_cache
        }

        def rollback_switch():
            # Take down anything that made it up under the new names.
            for n in list(devices_host.get_devices()):
                if n == name or n.startswith(name + "/"):
                    try:
                        devices_host.close_device(n)
                    except Exception:
                        logger.exception(
                            "Err closing device during rollback"
                        )
            gc.collect()
            time.sleep(0.01)
            gc.collect()

            switch.rollback_resources()

            for it in items:
                device_data_cache.pop(it["new_name"], None)

            # Cache entries for running devices were consumed at startup,
            # so rebuild them from the just-restored resources before the
            # parent plugin recreates its subdevices.
            for it in items:
                if it["old_data"] is not None:
                    device_data_cache[it["old_name"]] = (
                        it["old_m"],
                        it["old_r"],
                        copy.deepcopy(it["old_data"]["device"]),
                    )
            for n, v in old_cache.items():
                device_data_cache[n] = copy.deepcopy(v)

            switch.rollback_folders()

            if devname not in devices_host.devices:
                old_cfg = (
                    old_parent_rd["device"]
                    if old_parent_rd is not None
                    else savable_data
                )
                try:
                    # Best effort: this device was running moments ago.
                    makeDevice(
                        devname,
                        copy.deepcopy(old_cfg),
                        None,
                        parent_module,
                        parent_resource,
                    )
                except Exception:
                    logger.critical(
                        "Device update rollback restored the saved data but "
                        "could not restart the original device; it will "
                        "converge from disk on restart"
                    )

        try:
            # Stage 2: one switch of the whole tree.

            # The parent's close() cascades to all of its subdevices.
            devices_host.close_device(devname)
            for n in list(devices_host.get_devices()):
                if n.startswith(devname + "/"):
                    devices_host.close_device(n)

            gc.collect()
            time.sleep(0.01)
            time.sleep(0.01)
            gc.collect()

            switch.commit_folders()
            switch.commit_resources()

            for n in old_tree_names:
                device_data_cache.pop(n, None)
            for it in items:
                device_data_cache[it["new_name"]] = (
                    it["new_m"],
                    it["new_r"],
                    it["new_data"]["device"],
                )

            makeDevice(
                name,
                copy.deepcopy(savable_data),
                None,
                newparent_module,
                newparent_resource,
                strict_init=True,
            )
        except Exception:
            logger.exception("Device update failed; rolling back")
            try:
                rollback_switch()
            except Exception:
                logger.critical(
                    "Device update rollback encountered another error"
                )
            raise

        # Events must match the final set of visible devices exactly.
        final_visible = set(devices_host.get_devices())
        for n in sorted(initial_visible - final_visible):
            messagebus.post_message("/devices/removed/", n)
        for n in sorted(final_visible - initial_visible):
            messagebus.post_message("/devices/added/", n)

        switch.cleanup_folders()


def _update_subdevice_in_place(
    devname: str,
    runtime_obj: DeviceRuntimeState,
    savable_data: dict[str, Any],
    m: str,
    r: str | None,
):
    """Transactionally save a subdevice from its own edit page.

    The full name cannot change here (that would orphan it from its
    parent); the parent edit handles renames. Config, module/resource
    placement and the config folder are switched with rollback on error.
    """
    if savable_data["name"] != devname:
        raise ValueError(
            "Can't rename a subdevice directly; rename its parent device"
        )

    _check_device_type(runtime_obj, savable_data)
    savable_data["is_subdevice"] = True

    # A recreated subdevice's runtime container only carries the default
    # (possibly unstorable) resource path, so locate the persisted resource
    # by device name instead.
    old_m: str | None = None
    old_r: str | None = None
    old_rd = None
    for mm, mod in modules_state.ActiveModules.items():
        for rr, cand in mod.items():
            if (
                cand.get("resource", {}).get("type") == "device"
                and cand.get("device", {}).get("name") == devname
            ):
                old_m, old_r, old_rd = mm, rr, cand
                break
        if old_rd is not None:
            break

    if old_rd is None:
        # No saved resource (should be unusual); fall back to runtime attrs.
        old_m = runtime_obj.module_name
        old_r = runtime_obj.resource_name

    assert old_m and old_r

    new_m = m
    new_r = r or ".d/".join(devname.split("/"))

    item = _make_switch_item(
        devname,
        devname,
        old_m,
        old_r,
        new_m,
        new_r,
        old_rd,
        savable_data,
    )
    _validate_switch_items([item], {devname})

    switch = _DeviceTreeSwitch([item])
    try:
        switch.prepare_folders()
        switch.prepare_writes()
    except Exception:
        switch.abort_prepare()
        raise

    old_cache_entry: tuple[str, str, Mapping[str, Any]] | None = (
        copy.deepcopy(device_data_cache[devname])
        if devname in device_data_cache
        else None
    )
    old_attrs = (runtime_obj.module_name, runtime_obj.resource_name)

    try:
        switch.commit_folders()
        switch.commit_resources()

        device_data_cache[devname] = (new_m, new_r, savable_data)
        runtime_obj.module_name = new_m
        runtime_obj.resource_name = new_r
        if runtime_obj.device:
            runtime_obj.device.update_config(savable_data)
    except Exception:
        logger.exception("Subdevice update failed; rolling back")
        runtime_obj.module_name, runtime_obj.resource_name = old_attrs
        try:
            switch.rollback_resources()
            switch.rollback_folders()
        finally:
            if old_cache_entry is not None:
                device_data_cache[devname] = copy.deepcopy(old_cache_entry)
            else:
                device_data_cache.pop(devname, None)
        raise

    switch.cleanup_folders()


specialKeys = {
    "subclass",
    "name",
    "title",
    "type",
    "is_subdevice",
    "description",
    "notes",
}


class DeviceNamespace:
    Device = iot_devices.device.Device

    def __getitem__(self, name):
        d = devices_host.devices.get(name)
        if not d:
            raise KeyError(name)
        if not d.device:
            raise KeyError(name)
        if d.device.device_type == "unsupported":
            raise RuntimeError("There is no driver for this device")
        if d.device.device_type == "unknown":
            raise RuntimeError("There is no driver for this device")
        if d.device.device_type.lower() == "unusedsubdevice":
            raise RuntimeError("Unused subdevice config")

        return weakref.proxy(devices_host.devices[name])

    def __iter__(self):
        x = devices_host.get_devices()
        for i in x:
            try:
                x = self[i]
                yield i
            except (KeyError, RuntimeError):
                pass


def makeDevice(
    name: str,
    data: dict[str, Any],
    cls: type[iot_devices.device.Device] | None,
    module: str,
    resource: str,
    *,
    strict_init: bool = False,
) -> DeviceRuntimeState:
    """Create a device runtime.

    By default a driver/init failure is swallowed and the device is
    replaced with an UnsupportedDevice placeholder so the user can keep
    editing it.  With strict_init=True, a resolvable device class whose
    __init__ raises will propagate the exception instead (the missing
    driver case still falls back to the placeholder), allowing callers
    that are swapping devices to roll back.
    """
    err = None

    data = copy.deepcopy(data)
    data["name"] = name

    # We need to make sure we have a name

    # Cls lets us force make a device
    # of a different type for placeholders
    # if we can't support them yet
    if cls:
        data["type"] = cls.device_type

    else:
        try:
            cls = iot_devices.host.get_class(data)

            if not cls:
                raise ValueError("Couldn't get class")  # noqa: TRY301

        except KeyError:
            cls = UnsupportedDevice
        except ValueError:
            cls = UnsupportedDevice

        except Exception:
            cls = UnsupportedDevice
            logger.exception("Err creating device")
            err = traceback.format_exc()
            logger.exception("Error making device")

    new_data = copy.deepcopy(data)

    if new_data["name"] in devices_host.devices:
        raise RuntimeError(f"Device name already exists: {new_data['name']}")

    try:
        d = devices_host.add_device_from_class(
            cls,
            new_data,
            host_container_kwargs={"module": module, "resource": resource},
        )
    except Exception:
        if strict_init and cls is not UnsupportedDevice:
            raise
        d = devices_host.add_device_from_class(UnsupportedDevice, new_data)
        d.wait_device_ready().handle_exception()

    if err:
        if d.device:
            d.device.handle_error(err)
        else:
            logger.exception("Error making device")
    return d


def ensure_module_path_ok(module, resource):
    if resource.count("/"):
        dir = "/".join(resource.split("/")[:-1])
        for i in range(256):
            if dir in modules_state.ActiveModules[module]:
                if (
                    not modules_state.ActiveModules[module][dir]["resource"][
                        "type"
                    ]
                    == "directory"
                ):
                    raise RuntimeError(
                        f"File exists blocking creation of: {module}"
                    )
            if not dir.count("/"):
                break
            dir = "/".join(dir.split("/")[-1:])


@pydantic.validate_call
def storeDeviceInModule(d: dict[str, Any], module: str, resource: str) -> None:
    with modules_state.modulesLock:
        if resource.count("/"):
            dir = "/".join(resource.split("/")[:-1])
            for i in range(256):
                if dir not in modules_state.ActiveModules[module]:
                    r: modules_state.ResourceDictType = {
                        "resource": {
                            "type": "directory",
                            "modified": int(time.time()),
                        }
                    }

                    modules_state.raw_insert_resource(module, dir, r)
                if not dir.count("/"):
                    break
                dir = "/".join(dir.split("/")[:-1])

        if resource in modules_state.ActiveModules[module]:
            r = dict(
                copy.deepcopy(modules_state.ActiveModules[module][resource])
            )  # type: ignore #
        else:
            r = {
                "resource": {
                    "type": "device",
                    "modified": int(time.time()),
                }
            }

        r["device"] = d

        modules_state.raw_insert_resource(module, resource, r)


def getDeviceType(t):
    try:
        t = iot_devices.host.get_class({"type": t})
        if t:
            return t
        else:
            return UnsupportedDevice
    except Exception:
        logger.exception("Could not look up class")
        return UnsupportedDevice


def init_devices():
    # Load all the stuff from the modules

    # Sort so we get a deterministic-ish order
    with deffered_loaders_list_lock:
        deferred_loaders.sort()

    while deferred_loaders:
        try:
            deferred_loaders.pop(0)[2]()
        except Exception:
            logger.exception("Error loading device")
            messagebus.post_message(
                "/system/notifications/errors", "Error loading device"
            )
