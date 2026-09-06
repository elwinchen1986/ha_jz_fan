"""BLE communication core for the XD Smart Fan.

Reimplements the connect / discover / write / notify logic that the original
WeChat mini-program performed with wx.* BLE APIs, using bleak.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak_retry_connector import establish_connection

from .const import (
    CTRL_HEADER,
    IDX_GEAR,
    IDX_LIGHT,
    IDX_LR_SWING,
    IDX_MANUAL,
    IDX_MODE,
    IDX_POWER,
    IDX_TIMING,
    IDX_TRUMPET,
    IDX_UD_SWING,
    DEFAULT_POLL_INTERVAL,
    INIT_PACKETS,
    LR_SWING_ON_VALUE,
    NO_CHANGE,
    OFF_VALUE,
    ON_VALUE,
    QUERY_PACKET,
    WRITE_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class FanState:
    """Holds the last known state of the fan, mirrored from notify packets."""

    def __init__(self) -> None:
        self.power: bool = False
        self.gear: int = 1
        self.lr_swing: int = 0
        self.ud_swing: int = 0
        self.manual: int = 0
        self.mode: int = 1
        self.timing: int = 0
        self.light: bool = False
        self.trumpet: bool = False
        self.available: bool = False
        # Diagnostic counters / timestamps. These never influence behaviour
        # but let the logs prove the notify callback is actually being
        # invoked (and how often) on the BLE thread.
        self.notify_count: int = 0
        self.last_notify_at: float | None = None
        self.last_notify_hex: str = ""

    def as_dict(self) -> dict:
        return {
            "power": self.power,
            "gear": self.gear,
            "lr_swing": self.lr_swing,
            "ud_swing": self.ud_swing,
            "manual": self.manual,
            "mode": self.mode,
            "timing": self.timing,
            "light": self.light,
            "trumpet": self.trumpet,
            "available": self.available,
        }


def _decode_toggle(current: bool, raw: int) -> bool:
    """Decode a toggle byte from a notify packet.

    255 (0xFF) -> keep current, 2 -> on, otherwise off.
    Mirrors the mini-program logic: 255==keep, else (raw==2).
    """
    if raw == NO_CHANGE:
        return current
    return raw == 2


def _decode_value(current: int, raw: int) -> int:
    """Decode a numeric byte: 255 -> keep current, else raw."""
    if raw == NO_CHANGE:
        return current
    return raw


def _is_standard_service(uuid: str) -> bool:
    """Return True for the standard GATT services we should skip.

    0x1800 (Generic Access) and 0x1801 (Generic Attribute) never carry the
    fan's private control/notify characteristics. bleak may enumerate them at
    an index the mini-program assumed was a private service, so we skip them
    explicitly rather than relying on enumeration order.
    """
    u = str(uuid).lower()
    return u.startswith("00001800") or u.startswith("00001801") or u in (
        "1800",
        "1801",
    )


class XDFanController:
    """Manages a persistent BLE connection to one XD fan."""

    def __init__(
        self,
        device: BLEDevice,
        address: str,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._device = device
        self._address = address
        self._client: BleakClient | None = None
        # Primary write characteristic (first private-service writable char).
        self._write_char: BleakGATTCharacteristic | None = None
        # Every writable characteristic found across private services. The
        # mini-program wrote to *each* write characteristic of the target
        # service, so we mirror that "write to all" behaviour.
        self._write_chars: list[BleakGATTCharacteristic] = []
        # Every notify/indicate characteristic found across private services.
        # The mini-program subscribed to *all* of them (its global
        # onBLECharacteristicValueChange handler received data from any of
        # them), so we subscribe to all rather than guessing one.
        self._notify_chars: list[BleakGATTCharacteristic] = []
        self._lock = asyncio.Lock()
        self.state = FanState()
        self._update_callbacks: list[Callable[[], None]] = []
        self._poll_task: asyncio.Task | None = None
        self._poll_interval: float = poll_interval
        # The running HA event loop, captured on connect. BLE notify
        # callbacks fire on a different thread, so listeners must be
        # dispatched back onto this loop before touching HA state.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Monotonic timestamp captured at the start of each async_connect;
        # lets us print stage durations (handshake -> subscribe -> query ->
        # first notify) in the log.
        self._t_connect_start: float = 0.0

    # ---- public API -------------------------------------------------------

    @property
    def address(self) -> str:
        return self._address

    def register_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Register a listener invoked whenever state changes."""
        self._update_callbacks.append(cb)

        def _unsub() -> None:
            if cb in self._update_callbacks:
                self._update_callbacks.remove(cb)

        return _unsub

    def set_device(self, device: BLEDevice) -> None:
        """Refresh the BLEDevice reference (from HA bluetooth updates)."""
        self._device = device

    async def async_connect(self) -> None:
        """Establish connection, discover characteristics, subscribe, init.

        The whole method is heavily logged so a single HA log capture shows
        enough information to diagnose every common cause of a silent fan:
        wrong backend / MAC, no real command service, missing CCCD, wrong
        notify char, handshake never reached the device, query echo never
        arrived, etc. The logs are intentionally verbose.
        """
        self._t_connect_start = time.monotonic()
        _LOGGER.debug(
            "XD fan connect BEGIN address=%s device=%r details=%s rssi=%s",
            self._address,
            self._device,
            getattr(self._device, "details", None),
            getattr(self._device, "rssi", None),
        )
        # Remember the loop we run on so notify callbacks (fired on a
        # separate BLE thread)can be marshalled back safely.
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._client and self._client.is_connected:
                _LOGGER.debug(
                    "XD fan connect SKIP (already connected) address=%s",
                    self._address,
                )
                return
            t0 = time.monotonic()
            _LOGGER.debug(
                "XD fan establish_connection START backend=%s",
                type(self._device).__module__,
            )
            client = await establish_connection(
                BleakClient,
                self._device,
                self._address,
                disconnected_callback=self._on_disconnect,
            )
            _LOGGER.debug(
                "XD fan establish_connection DONE in %.2fs connected=%s",
                time.monotonic() - t0,
                client.is_connected,
            )
            self._client = client
            # BlueZ can return from establish_connection before the GATT
            # service table is resolved; discovering characteristics then
            # would select nothing and leave the fan silent. Wait first.
            await self._await_services_resolved(client)
            self._discover_characteristics(client)
            self.state.available = True
            self._notify_listeners()

            # Log link-level details that frequently explain a silent device:
            # the negotiated MTU (a tiny MTU can truncate the 15-byte status
            # frame), the backend class, and the confirmed connection state.
            mtu = getattr(client, "mtu_size", None)
            # BlueZ exposes the negotiated data length via the backend
            # adapter; not every backend exposes it, so guard with getattr.
            try:
                data_len = await self._read_data_length(client)
            except Exception as err:  # noqa: BLE001
                data_len = None
                _LOGGER.debug(
                    "XD fan could not read negotiated data length: %s", err
                )
            _LOGGER.debug(
                "XD fan link established: connected=%s mtu=%s "
                "data_len=%s backend=%s address=%s",
                client.is_connected,
                mtu,
                data_len,
                type(client).__module__,
                self._address,
            )

        # The mini-program's per-characteristic loop wrote the handshake
        # packets *before* it enabled notifications, then subscribed. Some
        # fan firmwares only switch into "report mode" after they receive the
        # handshake, so subscribing first (as we did previously) left the
        # CCCD enabled but the device never pushing. Mirror the app order:
        #   1. write handshake packets to every writable characteristic
        #   2. subscribe to every notify/indicate characteristic
        #   3. send the query packet so the device echoes its full state
        # Each write is paced by WRITE_DELAY (666ms) like the original app.

        # 1. Handshake first (device enters report mode).
        t_handshake_start = time.monotonic()
        _LOGGER.debug(
            "XD fan stage=HANDSHAKE packets=%d values=%s",
            len(INIT_PACKETS),
            [" ".join(f"{b:02x}" for b in p) for p in INIT_PACKETS],
        )
        for pkt in INIT_PACKETS:
            await self._write(pkt)
        _LOGGER.debug(
            "XD fan stage=HANDSHAKE DONE in %.2fs",
            time.monotonic() - t_handshake_start,
        )

        # 2. Subscribe to *every* notify/indicate characteristic. The
        # mini-program's global value-change handler received frames from any
        # of them, and on this device the echo can arrive on a notify
        # characteristic other than the one we would have guessed.
        t_subscribe_start = time.monotonic()
        _LOGGER.debug(
            "XD fan stage=SUBSCRIBE notify_chars=%s",
            [c.uuid for c in self._notify_chars],
        )
        async with self._lock:
            subscribed: list[str] = []
            for notify_char in self._notify_chars:
                t_sub = time.monotonic()
                # Sanity-check: does this char actually expose the CCCD
                # descriptor (0x2902)? If not, start_notify is a no-op and
                # the device will never push - log it loudly.
                has_cccd = any(
                    str(d.uuid).lower().startswith("00002902")
                    for d in notify_char.descriptors
                )
                _LOGGER.debug(
                    "XD fan subscribe uuid=%s props=%s has_cccd=%s "
                    "handle=%s",
                    notify_char.uuid,
                    ",".join(notify_char.properties),
                    has_cccd,
                    getattr(notify_char, "handle", "?"),
                )
                try:
                    await client.start_notify(notify_char, self._on_notify)
                    subscribed.append(notify_char.uuid)
                    # Read back the CCCD (0x2902) so the log shows whether
                    # the notification-enable bit is actually set on the
                    # device. Expected 01 00 (notify) or 02 00 (indicate);
                    # 00 00 means start_notify did NOT enable it.
                    cccd_val = await self._read_cccd(client, notify_char)
                    expected = "01 00"
                    ok = cccd_val is not None and (
                        cccd_val.replace(" ", "").lower() in ("0100", "0200")
                    )
                    _LOGGER.debug(
                        "XD fan notify enabled on %s, CCCD(0x2902)=%s "
                        "expected=%s match=%s in %.2fs",
                        notify_char.uuid,
                        cccd_val,
                        expected,
                        ok,
                        time.monotonic() - t_sub,
                    )
                    if not ok:
                        _LOGGER.warning(
                            "XD fan CCCD looks wrong on %s (got %s, want "
                            "01 00). Device may never push notifications.",
                            notify_char.uuid,
                            cccd_val,
                        )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "XD fan start_notify on %s failed after %.2fs: %s",
                        notify_char.uuid,
                        time.monotonic() - t_sub,
                        err,
                    )
            if subscribed:
                _LOGGER.debug(
                    "XD fan stage=SUBSCRIBE DONE in %.2fs subscribed=%s",
                    time.monotonic() - t_subscribe_start,
                    ", ".join(subscribed),
                )
            else:
                _LOGGER.error(
                    "XD fan has no notify characteristic subscribed; "
                    "state will not update"
                )

        # 3. Query the full status frame now that the device is in report
        # mode and we are listening. This is what populates the initial echo.
        t_query = time.monotonic()
        _LOGGER.debug(
            "XD fan stage=QUERY sending %s",
            bytes(QUERY_PACKET).hex(" "),
        )
        await self._write(QUERY_PACKET)
        _LOGGER.debug(
            "XD fan stage=QUERY sent in %.2fs, waiting for first notify...",
            time.monotonic() - t_query,
        )

        _LOGGER.debug(
            "XD fan connected and initialized: write=%s notify=%s "
            "subscribed=%s elapsed=%.2fs",
            getattr(self._write_char, "uuid", None),
            [c.uuid for c in self._notify_chars],
            len(self._notify_chars) > 0,
            time.monotonic() - self._t_connect_start,
        )

    async def async_disconnect(self) -> None:
        self.stop_polling()
        async with self._lock:
            if self._client and self._client.is_connected:
                try:
                    for notify_char in self._notify_chars:
                        try:
                            await self._client.stop_notify(notify_char)
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001
                    pass
                await self._client.disconnect()
            self._client = None
            self.state.available = False
            self._notify_listeners()

    async def async_query_state(self) -> None:
        """Ask the fan to report its full status frame.

        The device answers over the notify characteristic, which keeps Home
        Assistant in sync with changes made physically (buttons or remote).
        Safe to call repeatedly.
        """
        await self._write(QUERY_PACKET)

    def start_polling(self) -> None:
        """Start the periodic status-query loop (idempotent)."""
        if self._poll_interval <= 0:
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    def stop_polling(self) -> None:
        """Stop the periodic status-query loop."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    def set_poll_interval(self, seconds: float) -> None:
        """Update the poll interval; restart the loop if it is running."""
        self._poll_interval = seconds
        was_running = (
            self._poll_task is not None and not self._poll_task.done()
        )
        if was_running:
            self.stop_polling()
            self.start_polling()

    async def _poll_loop(self) -> None:
        """Periodically query fan status, reconnecting if the link drops.

        Each iteration logs whether the link is alive and which action it
        took (query / reconnect). The cumulative count of received
        notifies is logged too so a stuck poll is easy to spot.
        """
        _LOGGER.debug(
            "XD fan poll loop START interval=%.2fs", self._poll_interval
        )
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                connected = bool(
                    self._client and self._client.is_connected
                )
                _LOGGER.debug(
                    "XD fan poll tick: connected=%s notify_count=%d",
                    connected,
                    self.state.notify_count,
                )
                try:
                    if not connected:
                        _LOGGER.debug(
                            "XD fan poll: link not connected, reconnecting"
                        )
                        await self.async_connect()
                    else:
                        t_q = time.monotonic()
                        await self.async_query_state()
                        _LOGGER.debug(
                            "XD fan poll: query sent in %.2fs",
                            time.monotonic() - t_q,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "XD fan poll iteration failed: %s", err
                    )
        except asyncio.CancelledError:
            _LOGGER.debug("XD fan poll loop STOPPED")
            pass


    async def async_set_power(self, on: bool) -> None:
        self.state.power = on
        await self._send_field(IDX_POWER, ON_VALUE if on else OFF_VALUE)

    async def async_set_gear(self, gear: int) -> None:
        self.state.gear = gear
        await self._send_field(IDX_GEAR, gear)

    async def async_set_lr_swing(self, on: bool) -> None:
        # Use the strongest LR swing step (120°) when turning on, matching
        # the original app's "oscillate on" behaviour.
        value = LR_SWING_ON_VALUE if on else 0
        self.state.lr_swing = value
        await self._send_field(IDX_LR_SWING, value)

    async def async_set_lr_swing_value(self, value: int) -> None:
        """Set the left/right swing to a specific angle step (0..4)."""
        self.state.lr_swing = value
        await self._send_field(IDX_LR_SWING, value)

    async def async_set_ud_swing(self, value: int) -> None:
        self.state.ud_swing = value
        await self._send_field(IDX_UD_SWING, value)

    async def async_set_manual(self, direction: int) -> None:
        """Nudge the fan head in a direction (1=up 2=down 3=left 4=right)."""
        self.state.manual = direction
        await self._send_field(IDX_MANUAL, direction)

    async def async_set_mode(self, mode: int) -> None:
        self.state.mode = mode
        await self._send_field(IDX_MODE, mode)

    async def async_set_timing(self, hours: int) -> None:
        self.state.timing = hours
        await self._send_field(IDX_TIMING, hours)

    async def async_set_light(self, on: bool) -> None:
        self.state.light = on
        await self._send_field(IDX_LIGHT, ON_VALUE if on else OFF_VALUE)

    async def async_set_trumpet(self, on: bool) -> None:
        self.state.trumpet = on
        await self._send_field(IDX_TRUMPET, ON_VALUE if on else OFF_VALUE)

    # ---- internal helpers -------------------------------------------------

    async def _await_services_resolved(
        self,
        client: BleakClient,
        timeout: float = 10.0,
        interval: float = 0.25,
    ) -> None:
        """Wait until the GATT service table is actually resolved.

        With the BlueZ backend ``establish_connection`` can return before the
        peripheral's services have been resolved, in which case
        ``client.services`` is empty. Running characteristic discovery at that
        moment silently selects nothing -> no write char, no notify char, and
        the fan state never updates. So before discovering we poll until at
        least one service (with characteristics) shows up, or time out.
        """
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            services = list(client.services)
            has_chars = any(s.characteristics for s in services)
            if services and has_chars:
                _LOGGER.debug(
                    "XD fan services resolved: %d service(s) after %d attempt(s)",
                    len(services),
                    attempt,
                )
                return
            if time.monotonic() >= deadline:
                _LOGGER.warning(
                    "XD fan services still empty after %.1fs (%d attempts); "
                    "proceeding with discover anyway",
                    timeout,
                    attempt,
                )
                return
            _LOGGER.debug(
                "XD fan services not resolved yet (attempt %d, %d service(s)); "
                "waiting %.2fs",
                attempt,
                len(services),
                interval,
            )
            await asyncio.sleep(interval)

    def _discover_characteristics(self, client: BleakClient) -> None:
        """Pick the single private service the device really talks over.

        The WeChat mini-program only ever operated on *one* service -
        ``services[2]`` in its enumeration - and within it wrote the handshake
        to the (single) writable characteristic and subscribed to that
        service's notify characteristic. It never touched the other private
        services. Writing to / subscribing to the other services turned out to
        be counter-productive: the device only echoes its state over the one
        real command service, and touching the others can keep it silent.

        We cannot rely on enumeration index (bleak orders services differently
        from WeChat, and the order is not even stable between runs). Instead we
        identify the real service by its shape: on this hardware the command
        service exposes a **write-without-response** characteristic *and* a
     notify characteristic (e.g. service ae00 -> write ae01 / notify ae02),
        which is exactly what the mini-program used. We select that service and
        only write / subscribe within it, mirroring the app precisely. All
        other private services are ignored.
        """
        services = list(client.services)

        # Log the full GATT layout for troubleshooting, including each
        # characteristic's handle and its descriptors (so we can see whether a
        # CCCD 0x2902 exists on the notify characteristics - if it is missing
        # the device can never be told to start pushing notifications).
        for s_idx, service in enumerate(services):
            _LOGGER.debug(
                "XD fan service[%d] uuid=%s (%d chars)",
                s_idx,
                service.uuid,
                len(service.characteristics),
            )
            for char in service.characteristics:
                desc_uuids = [str(d.uuid) for d in char.descriptors]
                _LOGGER.debug(
                    "  char uuid=%s handle=%s props=%s descriptors=%s",
                    char.uuid,
                    getattr(char, "handle", "?"),
                    ",".join(char.properties),
                    desc_uuids or "none",
                )

        def _service_shape(service) -> tuple[
            BleakGATTCharacteristic | None,  # write-without-response char
            BleakGATTCharacteristic | None,  # write-with-response char
            BleakGATTCharacteristic | None,  # notify/indicate char
        ]:
            w_no_resp = w_resp = notify = None
            for char in service.characteristics:
                props = char.properties
                if "write-without-response" in props and w_no_resp is None:
                    w_no_resp = char
                elif "write" in props and w_resp is None:
                    w_resp = char
                if ("notify" in props or "indicate" in props) \
                        and notify is None:
                    notify = char
            return w_no_resp, w_resp, notify

        chosen_write: BleakGATTCharacteristic | None = None
        chosen_notify: BleakGATTCharacteristic | None = None
        chosen_uuid: str | None = None

        # 1. Preferred: a private service that has a write-without-response
        #    characteristic AND a notify characteristic (the app's service).
        for service in services:
            is_std = _is_standard_service(service.uuid)
            if is_std:
                _LOGGER.debug(
                    "XD fan discover SKIP standard service[%d]=%s",
                    service.uuid,
                )
                continue
            w_no_resp, w_resp, notify = _service_shape(service)
            _LOGGER.debug(
                "XD fan discover CANDIDATE service=%s "
                "w_no_resp=%s w_resp=%s notify=%s",
                service.uuid,
                getattr(w_no_resp, "uuid", None),
                getattr(w_resp, "uuid", None),
                getattr(notify, "uuid", None),
            )
            if w_no_resp is not None and notify is not None:
                chosen_write = w_no_resp
                chosen_notify = notify
                chosen_uuid = str(service.uuid)
                _LOGGER.debug(
                    "XD fan discover HIT-PREFERRED service=%s write=%s "
                    "notify=%s",
                    chosen_uuid,
                    chosen_write.uuid,
                    chosen_notify.uuid,
                )
                break
            else:
                _LOGGER.debug(
                    "XD fan discover MISS-PREFERRED service=%s (need both "
                    "write-without-response AND notify)",
                    service.uuid,
                )

        # 2. Fallback: any private service that has both a writable char (of
        #    either kind) and a notify char.
        if chosen_write is None:
            _LOGGER.debug(
                "XD fan discover entering FALLBACK-1 (any writable + notify)"
            )
            for service in services:
                if _is_standard_service(service.uuid):
                    continue
                w_no_resp, w_resp, notify = _service_shape(service)
                writable = w_no_resp or w_resp
                _LOGGER.debug(
                    "XD fan discover FALLBACK-1 CANDIDATE service=%s "
                    "writable=%s notify=%s",
                    service.uuid,
                    getattr(writable, "uuid", None),
                    getattr(notify, "uuid", None),
                )
                if writable is not None and notify is not None:
                    chosen_write = writable
                    chosen_notify = notify
                    chosen_uuid = str(service.uuid)
                    _LOGGER.debug(
                        "XD fan discover HIT-FALLBACK-1 service=%s "
                        "write=%s notify=%s",
                        chosen_uuid,
                        chosen_write.uuid,
                        chosen_notify.uuid,
                    )
                    break

        # 3. Last resort: first writable char anywhere + first notify anywhere.
        if chosen_write is None:
            _LOGGER.warning(
                "XD fan discover entering FALLBACK-2 (last resort: any "
                "writable anywhere + any notify anywhere) - state will be "
                "unreliable on this device"
            )
            for service in services:
                w_no_resp, w_resp, notify = _service_shape(service)
                writable = w_no_resp or w_resp
                if writable is not None and chosen_write is None:
                    chosen_write = writable
                    _LOGGER.debug(
                        "XD fan discover FALLBACK-2 picked writable=%s from "
                        "service=%s", writable.uuid, service.uuid,
                    )
                if notify is not None and chosen_notify is None:
                    chosen_notify = notify
                    _LOGGER.debug(
                        "XD fan discover FALLBACK-2 picked notify=%s from "
                        "service=%s", notify.uuid, service.uuid,
                    )

        # Hand the selected characteristic(s) back to the controller instance.
        # Without this assignment self._write_char stays None and
        # self._notify_chars stays empty, so async_connect logs
        # "notify_chars=[]" / "Write skipped, no write characteristic" and
        # no notify ever fires - which is why the device's accumulated-hour
        # field never updates. Mirroring the mini-program's behaviour: pick
        # the real command service and only operate on *its* chars.
        if chosen_write is not None:
            self._write_char = chosen_write
            self._write_chars = [chosen_write]
        if chosen_notify is not None:
            self._notify_chars = [chosen_notify]
        _LOGGER.debug(
            "XD fan discover RESULT write=%s notify=%s service=%s",
            getattr(self._write_char, "uuid", None),
            [c.uuid for c in self._notify_chars],
            chosen_uuid,
        )
        if self._write_char is None or not self._notify_chars:
            _LOGGER.error(
                "XD fan discover FAILED: no usable write+notify pair on "
                "device (write=%s notify=%s). Cannot control fan.",
                self._write_char,
                self._notify_chars,
            )

    def _build_packet(self, index: int, value: int) -> list[int]:
        """Build a full control packet with only one field changed."""
        payload = [NO_CHANGE] * 10  # bytes 5..14
        payload[index - IDX_POWER] = value
        return CTRL_HEADER + payload

    async def _send_field(self, index: int, value: int) -> None:
        packet = self._build_packet(index, value)
        await self._write(packet)
        self._notify_listeners()

    async def _write(self, packet: list[int]) -> None:
        async with self._lock:
            if not self._client or not self._client.is_connected:
                _LOGGER.warning("Write skipped, XD fan not connected")
                return
            if self._write_char is None:
                _LOGGER.warning("Write skipped, no write characteristic")
                return
            data = bytes(packet)
            targets = self._write_chars or [self._write_char]
            # We do not know which private service is the device's real
            # command channel, and the status echo only starts once the
            # handshake reaches it. So write the packet to *every* writable
            # characteristic; the device ignores frames on the wrong channel.
            for idx, wchar in enumerate(targets):
                props = wchar.properties
                use_response = "write" in props
                if not use_response and \
                        "write-without-response" not in props:
                    use_response = True
                t_w = time.monotonic()
                _LOGGER.debug(
                    "XD fan write (response=%s, len=%d) to %s: %s",
                    use_response,
                    len(data),
                    wchar.uuid,
                    data.hex(" "),
                )
                try:
                    await self._client.write_gatt_char(
                        wchar, data, response=use_response
                    )
                    _LOGGER.debug(
                        "XD fan write OK in %.2fs to %s",
                        time.monotonic() - t_w,
                        wchar.uuid,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning(
                        "XD fan write to %s failed (response=%s) -> %s; "
                        "retrying with response=%s",
                        wchar.uuid,
                        use_response,
                        err,
                        not use_response,
                    )
                    try:
                        await self._client.write_gatt_char(
                            wchar, data, response=not use_response
                        )
                        _LOGGER.debug(
                            "XD fan write RETRY OK in %.2fs to %s",
                            time.monotonic() - t_w,
                            wchar.uuid,
                        )
                    except Exception as err2:  # noqa: BLE001
                        _LOGGER.error(
                            "XD fan write to %s retry also failed: %s",
                            wchar.uuid,
                            err2,
                        )
                if idx < len(targets) - 1:
                    await asyncio.sleep(0.1)
        # Pace consecutive writes like the original app.
        await asyncio.sleep(WRITE_DELAY)

    async def _read_data_length(
        self, client: BleakClient
    ) -> int | None:
        """Read the negotiated data length (BlueZ only).

        On Linux the BlueZ adapter exposes ``read_data_length``; on macOS /
        Windows the value is implicit in the MTU. We swallow all errors so a
        backend without this API just returns None.
        """
        for path in ("read_data_length", "acquire_data_length"):
            fn = getattr(client, path, None)
            if fn is None:
                continue
            try:
                value = await fn()
                return int(value)
            except Exception:  # noqa: BLE001
                continue
        # As a last resort try the platform-specific adapter attribute.
        adapter = getattr(client, "_adapter", None) or getattr(
            client, "adapter", None
        )
        if adapter is not None:
            for path in ("read_data_length", "acquire_data_length"):
                fn = getattr(adapter, path, None)
                if fn is None:
                    continue
                try:
                    value = await fn()
                    return int(value)
                except Exception:  # noqa: BLE001
                    continue
        return None

    def _schedule_first_notify_watchdog(self) -> None:
        """Log a warning if no notify arrives within 5s of connect/query.

        The fan is *required* to echo its full state immediately after
        receiving the query packet (cmd 0x10). Silence at this point is
        the classic "wrong service / characteristic" symptom, so we make
        it loud in the log.
        """
        delay = 5.0

        async def _watch() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            count = self.state.notify_count
            if count == 0:
                _LOGGER.warning(
                    "XD fan NO NOTIFY within %.1fs after query "
                    "(subscribed=%s, chosen_write=%s, chosen_notify=%s, "
                    "cccd_state=%s). Likely causes: wrong service/char, "
                    "handshake did not reach the device, or bleak backend "
                    "did not enable CCCD.",
                    delay,
                    [c.uuid for c in self._notify_chars],
                    getattr(self._write_char, "uuid", None),
                    [c.uuid for c in self._notify_chars],
                    "unknown",
                )
            else:
                _LOGGER.debug(
                    "XD fan first notify OK: count=%d last_hex=%s",
                    count,
                    self.state.last_notify_hex,
                )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(_watch())

    async def _enable_cccd(
        self, client: BleakClient, notify_char: BleakGATTCharacteristic
    ) -> None:
        """Explicitly write the Client Characteristic Config descriptor.

        Some bleak backends do not reliably write the CCCD (0x2902) when
        start_notify is called, so the device never actually starts pushing
        notifications. We locate the 0x2902 descriptor on the characteristic
        and write [0x01, 0x00] (notify) or [0x02, 0x00] (indicate). Failures
        are non-fatal: many backends already handled it inside start_notify.
        """
        try:
            cccd = None
            for descriptor in notify_char.descriptors:
                if str(descriptor.uuid).lower().startswith("00002902"):
                    cccd = descriptor
                    break
            if cccd is None:
                return
            if "indicate" in notify_char.properties:
                value = bytes([0x02, 0x00])
            else:
                value = bytes([0x01, 0x00])
            await client.write_gatt_descriptor(cccd.handle, value)
            _LOGGER.debug(
                "XD fan CCCD written on %s: %s",
                notify_char.uuid,
                value.hex(" "),
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "XD fan CCCD write on %s skipped: %s",
                notify_char.uuid,
                err,
            )

    async def _read_cccd(
        self, client: BleakClient, notify_char: BleakGATTCharacteristic
    ) -> str | None:
        """Read back the CCCD (0x2902) descriptor for diagnostics.

        After ``start_notify`` we want to confirm that the device really has
        notifications turned on. The value is two bytes:

          01 00 -> notify enabled
          02 00 -> indicate enabled
          00 00 -> neither (the device will never push)

        We only ever read; we never write the CCCD directly because bleak
        disallows that and ``start_notify`` should already have written it.
        Returns a hex string (e.g. ``"01 00"``) or ``None`` if the CCCD is
        not present / the read failed.
        """
        try:
            cccd = None
            for descriptor in notify_char.descriptors:
                if str(descriptor.uuid).lower().startswith("00002902"):
                    cccd = descriptor
                    break
            if cccd is None:
                _LOGGER.debug(
                    "XD fan no CCCD(0x2902) descriptor on %s",
                    notify_char.uuid,
                )
                return None
            value = await client.read_gatt_descriptor(cccd.handle)
            _LOGGER.debug(
                "XD fan CCCD(0x2902) on %s raw=%s",
                notify_char.uuid,
                bytes(value).hex(" "),
            )
            return bytes(value).hex(" ")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "XD fan CCCD read on %s failed: %s",
                notify_char.uuid,
                err,
            )
            return None

    def _on_notify(
        self, _char: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Handle a notify packet and update state.

        Runs on the bleak backend thread, NOT the HA event loop. Logging is
        thread-safe but state listeners are marshalled onto the HA loop
        via ``_notify_listeners``.
        """
        now = time.monotonic()
        self.state.notify_count += 1
        self.state.last_notify_at = now
        self.state.last_notify_hex = bytes(data).hex(" ")
        since_connect = (
            now - self._t_connect_start
            if self._t_connect_start > 0
            else -1.0
        )
        _LOGGER.debug(
            "XD fan notify recv #%d char=%s len=%d since_connect=%.2fs "
            "data=%s",
            self.state.notify_count,
            getattr(_char, "uuid", "?"),
            len(data),
            since_connect,
            bytes(data).hex(" "),
        )
        n = list(data)
        if len(n) < 15:
            _LOGGER.warning(
                "XD fan short notify frame (%d<15), skipping: %s "
                "(expected header AA 55 10 00 0A + 10 payload bytes)",
                len(n),
                bytes(data).hex(" "),
            )
            return
        # Confirm the frame really is the XD status frame (header match);
        # otherwise the device may be echoing a different protocol.
        if n[:5] != [0xAA, 0x55, 0x10, 0x00, 0x0A]:
            _LOGGER.warning(
                "XD fan notify with UNEXPECTED HEADER %s (want AA 55 10 00 "
                "0A). Device may be echoing a different protocol.",
                bytes(data[:5]).hex(" "),
            )
        # Echo-mode detector: a frame whose payload bytes are mostly 0xFF
        # (NO_CHANGE) means the device is echoing what we last wrote rather
        # than reporting real state. This is one of the only ways to spot
        # "notify arrives but state never changes" at a glance.
        payload = n[IDX_POWER:]
        no_change_count = sum(1 for b in payload if b == NO_CHANGE)
        if no_change_count >= 8:
            _LOGGER.warning(
                "XD fan notify ECHO-MODE: %d/15 payload bytes are NO_CHANGE "
                "(0xFF) — device is echoing the last write, NOT reporting "
                "real state. State will NOT update from this frame. "
                "raw=%s",
                no_change_count,
                bytes(data).hex(" "),
            )
        before = self.state.as_dict()
        s = self.state
        s.power = _decode_toggle(s.power, n[IDX_POWER])
        s.gear = _decode_value(s.gear, n[IDX_GEAR])
        s.lr_swing = _decode_value(s.lr_swing, n[IDX_LR_SWING])
        s.ud_swing = _decode_value(s.ud_swing, n[IDX_UD_SWING])
        s.manual = _decode_value(s.manual, n[IDX_MANUAL])
        s.mode = _decode_value(s.mode, n[IDX_MODE])
        s.timing = _decode_value(s.timing, n[IDX_TIMING])
        s.light = _decode_toggle(s.light, n[IDX_LIGHT])
        s.trumpet = _decode_toggle(s.trumpet, n[IDX_TRUMPET])
        after = self.state.as_dict()
        # Per-field diff so a "stuck on NO_CHANGE" path is visible.
        changes = {
            k: (before.get(k), after.get(k))
            for k in after
            if before.get(k) != after.get(k)
        }
        _LOGGER.debug(
            "XD fan state updated: %s diff=%s raw_bytes=%s",
            after,
            changes or "{}",
            n[IDX_POWER:],
        )
        self._notify_listeners()

    
    def _on_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("XD fan %s disconnected", self._address)
        self.state.available = False
        self._notify_listeners()

    def _notify_listeners(self) -> None:
        """Notify listeners on the HA event loop.

        BLE notifications arrive on a background thread, but the listeners
        call ``async_write_ha_state()`` which is *not* thread-safe and must
        run on the event loop. When a loop is known, marshal the callbacks
        onto it with ``call_soon_threadsafe``; otherwise call directly (e.g.
        before the first connection).
        """
        loop = self._loop
        callbacks = list(self._update_callbacks)

        def _run() -> None:
            for cb in callbacks:
                cb()

        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(_run)
        else:
            _run()