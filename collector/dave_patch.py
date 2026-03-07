"""DAVE (Discord's Audio/Video End-to-End Encryption) protocol patch for py-cord 2.7.x.

py-cord 2.7.1 does not implement the DAVE/MLS protocol, which Discord requires
for voice connections in servers with E2E encryption enabled (WebSocket close code 4017).

This module monkey-patches py-cord's DiscordVoiceWebSocket and VoiceClient to add
DAVE/MLS support, ported from discord.py 2.7.1 (MIT License, © Rapptz).

Usage:
    from collector.dave_patch import apply_dave_patch
    apply_dave_patch()  # call before bot.run()
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Dict

_log = logging.getLogger(__name__)

try:
    import davey as _davey
    _has_dave = True
except ImportError:
    _davey = None  # type: ignore[assignment]
    _has_dave = False


def apply_dave_patch() -> bool:
    """Monkey-patch py-cord's DiscordVoiceWebSocket to support the DAVE protocol.

    Returns True if patch was successfully applied, False if davey is not installed.
    """
    if not _has_dave:
        _log.warning(
            "davey library not installed — DAVE patch skipped. "
            "Voice connections to servers requiring DAVE will fail (close code 4017). "
            "Fix: pip install 'davey>=0.1.0'"
        )
        return False

    import discord.gateway as _gw
    import discord.voice_client as _vc
    from discord.gateway import VoiceKeepAliveHandler

    VWS = _gw.DiscordVoiceWebSocket
    VoiceClient = _vc.VoiceClient

    # ── DAVE/MLS opcode constants ────────────────────────────────────────────
    VWS.DAVE_PREPARE_TRANSITION        = 21
    VWS.DAVE_EXECUTE_TRANSITION        = 22
    VWS.DAVE_TRANSITION_READY          = 23
    VWS.DAVE_PREPARE_EPOCH             = 24
    VWS.MLS_EXTERNAL_SENDER            = 25
    VWS.MLS_KEY_PACKAGE                = 26
    VWS.MLS_PROPOSALS                  = 27
    VWS.MLS_COMMIT_WELCOME             = 28
    VWS.MLS_ANNOUNCE_COMMIT_TRANSITION = 29
    VWS.MLS_WELCOME                    = 30
    VWS.MLS_INVALID_COMMIT_WELCOME     = 31

    # ── DAVE state on VoiceClient ────────────────────────────────────────────

    VoiceClient.max_dave_protocol_version = property(  # type: ignore[attr-defined]
        lambda self: _davey.DAVE_PROTOCOL_VERSION
    )

    async def _reinit_dave_session(self, ws=None) -> None:
        # ws is the DiscordVoiceWebSocket to send on.
        # During connect_websocket()'s polling loop, client.ws is still MISSING,
        # so callers inside received_message pass the VWS explicitly.
        _ws = ws if ws is not None else self.ws
        if self.dave_protocol_version > 0:
            if self.dave_session is not None:
                self.dave_session.reinit(
                    self.dave_protocol_version, self.user.id, self.channel.id
                )
            else:
                self.dave_session = _davey.DaveSession(
                    self.dave_protocol_version, self.user.id, self.channel.id
                )
            if self.dave_session is not None:
                await _ws.send_binary(
                    VWS.MLS_KEY_PACKAGE,
                    self.dave_session.get_serialized_key_package(),
                )
        elif self.dave_session:
            self.dave_session.reset()
            self.dave_session.set_passthrough_mode(True, 10)

    VoiceClient.reinit_dave_session = _reinit_dave_session  # type: ignore[attr-defined]

    async def _execute_transition(self, transition_id: int) -> None:
        if transition_id not in self.dave_pending_transitions:
            _log.warning("Received execute transition for unknown id %d", transition_id)
            return
        old_version = self.dave_protocol_version
        self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)
        if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
            _log.debug("DAVE session downgraded to passthrough")
        _log.debug(
            "DAVE transition %d executed (protocol_version=%d)",
            transition_id, self.dave_protocol_version,
        )

    VoiceClient._execute_transition = _execute_transition  # type: ignore[attr-defined]

    async def _recover_from_invalid_commit(self, transition_id: int) -> None:
        _log.warning("Recovering from invalid MLS commit (transition_id=%d)", transition_id)
        await self.ws.send_as_json({
            "op": VWS.MLS_INVALID_COMMIT_WELCOME,
            "d": {"transition_id": transition_id},
        })
        await self.reinit_dave_session()

    VoiceClient._recover_from_invalid_commit = _recover_from_invalid_commit  # type: ignore[attr-defined]

    def _init_dave_state(client) -> None:
        """Lazily initialise DAVE state fields on a VoiceClient instance."""
        if not hasattr(client, "dave_session"):
            client.dave_session = None
            client.dave_protocol_version = 0
            client.dave_pending_transitions: Dict[int, int] = {}

    # ── send_binary ──────────────────────────────────────────────────────────

    async def send_binary(self, opcode: int, data: bytes) -> None:
        _log.debug("Sending voice binary frame: opcode=%d, size=%d", opcode, len(data))
        await self.ws.send_bytes(bytes([opcode]) + data)

    VWS.send_binary = send_binary  # type: ignore[attr-defined]

    # ── send_transition_ready ─────────────────────────────────────────────────

    async def send_transition_ready(self, transition_id: int) -> None:
        await self.send_as_json({
            "op": VWS.DAVE_TRANSITION_READY,
            "d": {"transition_id": transition_id},
        })

    VWS.send_transition_ready = send_transition_ready  # type: ignore[attr-defined]

    # ── identify (adds max_dave_protocol_version) ─────────────────────────────

    async def identify(self) -> None:
        state = self._connection
        _init_dave_state(state)
        payload = {
            "op": VWS.IDENTIFY,
            "d": {
                "server_id":                str(state.server_id),
                "user_id":                  str(state.user.id),
                "session_id":               state.session_id,
                "token":                    state.token,
                "max_dave_protocol_version": state.max_dave_protocol_version,
            },
        }
        await self.send_as_json(payload)

    VWS.identify = identify  # type: ignore[method-assign]

    # ── received_message (replaces original; adds DAVE opcode handling) ───────

    async def received_message(self, msg) -> None:
        _log.debug("Voice websocket frame received: %s", msg)
        op   = msg["op"]
        data = msg.get("d") or {}

        if isinstance(data, dict):
            self.seq_ack = data.get("seq", self.seq_ack)

        client = self._connection
        _init_dave_state(client)

        if op == VWS.READY:
            await self.initial_connection(data)

        elif op == VWS.HEARTBEAT_ACK:
            self._keep_alive.ack()

        elif op == VWS.RESUMED:
            _log.info("Voice RESUME succeeded.")

        elif op == VWS.SESSION_DESCRIPTION:
            client.mode = data["mode"]
            await self.load_secret_key(data)
            dave_version = data.get("dave_protocol_version", 0)
            client.dave_protocol_version = dave_version
            if dave_version > 0:
                # Pass self (VWS) explicitly: during connect_websocket()'s
                # polling loop, client.ws is still MISSING.
                await client.reinit_dave_session(ws=self)

        elif op == VWS.HELLO:
            interval = data["heartbeat_interval"] / 1000.0
            self._keep_alive = VoiceKeepAliveHandler(ws=self, interval=min(interval, 5.0))
            self._keep_alive.start()

        elif op == VWS.SPEAKING:
            ssrc     = data["ssrc"]
            user     = int(data["user_id"])
            speaking = data["speaking"]
            if ssrc in self.ssrc_map:
                self.ssrc_map[ssrc]["speaking"] = speaking
            else:
                self.ssrc_map[ssrc] = {"user_id": user, "speaking": speaking}

        elif op == VWS.DAVE_PREPARE_TRANSITION:
            transition_id    = data["transition_id"]
            protocol_version = data["protocol_version"]
            _log.debug(
                "DAVE_PREPARE_TRANSITION id=%d protocol_version=%d",
                transition_id, protocol_version,
            )
            client.dave_pending_transitions[transition_id] = protocol_version
            if transition_id == 0:
                await client._execute_transition(transition_id)
            else:
                if protocol_version == 0 and client.dave_session:
                    client.dave_session.set_passthrough_mode(True, 120)
                await self.send_transition_ready(transition_id)

        elif op == VWS.DAVE_EXECUTE_TRANSITION:
            _log.debug("DAVE_EXECUTE_TRANSITION id=%d", data["transition_id"])
            await client._execute_transition(data["transition_id"])

        elif op == VWS.DAVE_PREPARE_EPOCH:
            epoch = data.get("epoch", 0)
            _log.debug("DAVE_PREPARE_EPOCH epoch=%d", epoch)
            if epoch == 1:
                client.dave_protocol_version = data["protocol_version"]
                await client.reinit_dave_session(ws=self)

        await self._hook(self, msg)

    VWS.received_message = received_message  # type: ignore[method-assign]

    # ── received_binary_message (new — handles MLS binary frames) ────────────

    async def received_binary_message(self, msg: bytes) -> None:
        self.seq_ack = struct.unpack_from(">H", msg, 0)[0]
        op = msg[2]
        _log.debug("Voice binary frame received: %d bytes, op=%d", len(msg), op)

        client = self._connection
        if not hasattr(client, "dave_session") or client.dave_session is None:
            return

        if op == VWS.MLS_EXTERNAL_SENDER:
            client.dave_session.set_external_sender(msg[3:])
            _log.debug("MLS_EXTERNAL_SENDER processed")

        elif op == VWS.MLS_PROPOSALS:
            optype = msg[3]
            result = client.dave_session.process_proposals(
                _davey.ProposalsOperationType.append if optype == 0
                else _davey.ProposalsOperationType.revoke,
                msg[4:],
            )
            if isinstance(result, _davey.CommitWelcome):
                payload = result.commit + result.welcome if result.welcome else result.commit
                await self.send_binary(VWS.MLS_COMMIT_WELCOME, payload)
            _log.debug("MLS_PROPOSALS processed")

        elif op == VWS.MLS_ANNOUNCE_COMMIT_TRANSITION:
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            try:
                client.dave_session.process_commit(msg[5:])
                if transition_id != 0:
                    client.dave_pending_transitions[transition_id] = client.dave_protocol_version
                    await self.send_transition_ready(transition_id)
                _log.debug("MLS_ANNOUNCE_COMMIT_TRANSITION processed, id=%d", transition_id)
            except Exception:
                _log.exception("Failed to process MLS commit (transition_id=%d)", transition_id)
                await client._recover_from_invalid_commit(transition_id)

        elif op == VWS.MLS_WELCOME:
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            try:
                client.dave_session.process_welcome(msg[5:])
                if transition_id != 0:
                    client.dave_pending_transitions[transition_id] = client.dave_protocol_version
                    await self.send_transition_ready(transition_id)
                _log.debug("MLS_WELCOME processed, id=%d", transition_id)
            except Exception:
                _log.exception("Failed to process MLS welcome (transition_id=%d)", transition_id)
                await client._recover_from_invalid_commit(transition_id)

    VWS.received_binary_message = received_binary_message  # type: ignore[attr-defined]

    # ── poll_event (adds BINARY frame dispatch) ───────────────────────────────

    import aiohttp as _aiohttp
    from discord.errors import ConnectionClosed as _ConnectionClosed
    import discord.utils as _utils

    async def poll_event(self) -> None:
        msg = await asyncio.wait_for(self.ws.receive(), timeout=30.0)
        if msg.type is _aiohttp.WSMsgType.TEXT:
            await self.received_message(_utils._from_json(msg.data))
        elif msg.type is _aiohttp.WSMsgType.BINARY:
            await self.received_binary_message(msg.data)
        elif msg.type is _aiohttp.WSMsgType.ERROR:
            _log.debug("Received voice WS error: %s", msg)
            raise _ConnectionClosed(self.ws, shard_id=None) from msg.data
        elif msg.type in (
            _aiohttp.WSMsgType.CLOSED,
            _aiohttp.WSMsgType.CLOSE,
            _aiohttp.WSMsgType.CLOSING,
        ):
            _log.debug("Received voice WS close: %s", msg)
            raise _ConnectionClosed(self.ws, shard_id=None, code=self._close_code)

    VWS.poll_event = poll_event  # type: ignore[method-assign]

    # ── on_voice_server_update (DAVE reconnect detection) ────────────────────
    #
    # When Discord updates the voice server while connected (e.g. after the MLS
    # key exchange completes), py-cord's default handler closes the WS with code
    # 4000 and falls through to a full reconnect that calls voice_disconnect() +
    # sane_wait_for([_voice_state_complete, _voice_server_complete], timeout=60).
    # Discord never sends fresh voice state events during a DAVE-triggered
    # reconnect, so sane_wait_for times out.
    #
    # Fix: when DAVE is active, set _dave_reconnecting=True so poll_voice_ws
    # skips voice_disconnect() and only reconnects the voice WebSocket.

    import socket as _socket_mod
    from discord.backoff import ExponentialBackoff as _ExponentialBackoff
    from discord.utils import MISSING as _MISSING

    async def on_voice_server_update(self, data) -> None:
        if self._voice_server_complete.is_set():
            _log.info("Ignoring extraneous voice server update.")
            return

        self.token = data.get("token")
        self.server_id = int(data["guild_id"])
        endpoint = data.get("endpoint")

        if endpoint is None or self.token is None:
            _log.warning(
                "Awaiting endpoint... This requires waiting. "
                "If timeout occurred considering raising the timeout and reconnecting."
            )
            return

        self.endpoint = endpoint.removeprefix("wss://")
        self.endpoint_ip = _MISSING

        if self.socket and self.socket is not _MISSING:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket = _socket_mod.socket(_socket_mod.AF_INET, _socket_mod.SOCK_DGRAM)
        self.socket.setblocking(False)

        if not self._handshaking:
            if hasattr(self, "dave_protocol_version") and self.dave_protocol_version > 0:
                _log.info(
                    "DAVE-triggered voice server update — "
                    "will reconnect voice WS only (skipping full voice_disconnect)"
                )
                self._dave_reconnecting = True
            else:
                _log.info(
                    "Voice server update while connected — closing WS with code 4000"
                )
            await self.ws.close(4000)
            return

        self._voice_server_complete.set()

    VoiceClient.on_voice_server_update = on_voice_server_update  # type: ignore[attr-defined]

    # ── unpack_audio (add DAVE E2EE decryption layer) ─────────────────────────
    #
    # Discord audio has two encryption layers:
    #   1. XSalsa20/AEAD  — handled by py-cord's _decrypt_* methods in RawData
    #   2. DAVE/MLS E2EE  — applied on top; must be decrypted BEFORE Opus decode
    #
    # We override unpack_audio to inject davey.DaveSession.decrypt() between
    # the two layers when a ready DAVE session is present.

    _orig_unpack_audio = VoiceClient.unpack_audio

    def unpack_audio(self, data: bytes) -> None:
        if data[1] & 0x78 != 0x78:
            return
        if self.paused:
            return

        from discord.sinks import RawData

        raw = RawData(data, self)

        # DAVE E2EE decryption (layer 2, after XSalsa20 layer 1)
        dave_session = getattr(self, "dave_session", None)
        if dave_session is not None and dave_session.ready:
            ssrc_info = self.ws.ssrc_map.get(raw.ssrc, {})
            user_id = ssrc_info.get("user_id")
            if user_id is None:
                # ssrc_map not yet populated (DAVE reconnect or first SPEAKING
                # event not received yet).  Passing DAVE-encrypted bytes to
                # Opus would produce "corrupted stream" — drop instead.
                _log.debug(
                    "DAVE active but no user_id for ssrc=%d, dropping packet",
                    raw.ssrc,
                )
                return
            if not dave_session.can_passthrough(user_id):
                try:
                    raw.decrypted_data = dave_session.decrypt(
                        user_id,
                        _davey.MediaType.audio,
                        raw.decrypted_data,
                    )
                except Exception:
                    _log.debug(
                        "DAVE audio decryption failed for ssrc=%d, dropping packet",
                        raw.ssrc,
                    )
                    return

        if raw.decrypted_data == b"\xf8\xff\xfe":  # silence frame
            return

        self.decoder.decode(raw)

    VoiceClient.unpack_audio = unpack_audio  # type: ignore[method-assign]

    # ── poll_voice_ws (handle DAVE WS-only reconnect) ────────────────────────
    #
    # When _dave_reconnecting is True and code 4000 arrives, skip voice_disconnect
    # + connect() and instead call connect_websocket() directly with the already-
    # received new endpoint/token.  This mirrors discord.py's behaviour.

    async def poll_voice_ws(self, reconnect: bool) -> None:
        backoff = _ExponentialBackoff()
        while True:
            try:
                await self.ws.poll_event()
            except (_ConnectionClosed, asyncio.TimeoutError) as exc:
                if isinstance(exc, _ConnectionClosed):
                    if exc.code == 1000:
                        _log.info(
                            "Disconnecting from voice normally, close code %d.",
                            exc.code,
                        )
                        await self.disconnect()
                        break

                    if exc.code == 4014:
                        _log.info(
                            "Disconnected from voice by force... potentially reconnecting."
                        )
                        successful = await self.potential_reconnect()
                        if successful:
                            continue
                        _log.info(
                            "Reconnect was unsuccessful, disconnecting from voice normally..."
                        )
                        await self.disconnect()
                        break

                    if exc.code == 4015:
                        _log.info("Disconnected from voice, trying to resume...")
                        try:
                            await self.ws.resume()
                        except asyncio.TimeoutError:
                            _log.info(
                                "Could not resume the voice connection... Disconnection..."
                            )
                            if self._connected.is_set():
                                await self.disconnect(force=True)
                        else:
                            _log.info("Successfully resumed voice connection")
                            continue

                    # DAVE reconnect: only reconnect voice WS, no voice_disconnect
                    if exc.code == 4000 and getattr(self, "_dave_reconnecting", False):
                        self._dave_reconnecting = False
                        # Preserve ssrc_map so audio decryption keeps working
                        # after the new VWS starts (SPEAKING events haven't
                        # repopulated it yet).
                        old_ssrc_map = getattr(self.ws, "ssrc_map", {}).copy()
                        _log.info(
                            "DAVE reconnect: reconnecting voice WebSocket only "
                            "(endpoint=%s, carrying %d ssrc entries)",
                            self.endpoint, len(old_ssrc_map),
                        )
                        try:
                            self.ws = await self.connect_websocket()
                            if old_ssrc_map:
                                self.ws.ssrc_map.update(old_ssrc_map)
                            _log.info("DAVE voice WS reconnect successful")
                            continue
                        except (_ConnectionClosed, asyncio.TimeoutError):
                            _log.exception(
                                "DAVE voice WS reconnect failed, "
                                "falling back to full reconnect"
                            )
                            # fall through to generic reconnect below

                if not reconnect:
                    await self.disconnect()
                    raise

                retry = backoff.delay()
                _log.exception(
                    "Disconnected from voice... Reconnecting in %.2fs.", retry
                )
                self._connected.clear()
                await asyncio.sleep(retry)
                await self.voice_disconnect()
                try:
                    await self.connect(reconnect=True, timeout=self.timeout)
                except asyncio.TimeoutError:
                    _log.warning("Could not connect to voice... Retrying...")
                    continue

    VoiceClient.poll_voice_ws = poll_voice_ws  # type: ignore[method-assign]

    _log.info(
        "DAVE patch applied to py-cord DiscordVoiceWebSocket "
        "(DAVE protocol version %d)",
        _davey.DAVE_PROTOCOL_VERSION,
    )
    return True
