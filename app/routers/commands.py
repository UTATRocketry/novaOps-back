from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.context import AppContext
from app.deps import get_context
from app.models import (
    CONSOLE_ACTIONS,
    CommandPayload,
    DirectRelayPayload,
    DirectServoPayload,
    FasAuxPayload,
    FasBuzzerPayload,
    FasChargerPayload,
    FasRabPayload,
    FasRadioConfigPayload,
    FasRuncamRecordPayload,
    FasSdPayload,
    FasSoundPayload,
    SystemCommandPayload,
)
from app.services.role_service import ClientRole
from app.services.sound import (
    SND_FMT_IMA_ADPCM,
    SND_FMT_PCM_S16,
    TranscodeError,
    audio_to_clip,
    have_ffmpeg,
)

# Clip bytes no longer ride the MQTT command (the bridge downloads them from
# /api/fas/sound/clip/{token}), so these limits are only sanity guards against a
# clip that could never fit the FMC's flash or would exhaust backend memory.
# The real ceiling is the soundboard's free space, checked below against the
# cap_kb/used_kb the FMC reports in flight telemetry.
SOUND_CLIP_MAX_BYTES = int(os.getenv("NOVA_SOUND_CLIP_MAX_BYTES", str(8 * 1024 * 1024)))
SOUND_SOURCE_MAX_BYTES = int(os.getenv("NOVA_SOUND_SOURCE_MAX_BYTES", str(64 * 1024 * 1024)))
_SOURCE_READ_CHUNK = 1 << 20

router = APIRouter(prefix="/api", tags=["Commands"])


@router.post(
    "/commands",
    summary="Send actuator command",
    description=(
        "Translate and publish a client actuator command over MQTT. "
        "Role is checked via `X-Client-Id` header: viewers are blocked; "
        "pad role is restricted to safety-critical commands."
    ),
)
async def post_command(
    payload: CommandPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    try:
        ctx.role_service.check_command_role(caller_role, payload.name, payload.state, ctx.config_service.config)
        commands = await ctx.command_service.apply_command(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"published_commands": commands}


@router.post("/console", summary="Send raw console payload", description="Publish an unformatted console payload to nova/console.")
async def post_console(payload: dict, ctx: AppContext = Depends(get_context)) -> dict:
    ctx.mqtt_service.publish_console(payload)
    return {"published": True}


@router.post(
    "/console/command",
    summary="Send a FAS console command",
    description=(
        "Publish a console control/TX command to the device command topic so the "
        "FAS bridge acts on it. Supports actions: start, stop, list_ports, "
        "configure, disconnect, status, tx. Requires operator or admin role (raw frame TX is "
        "powerful). Output is delivered back over the WebSocket console stream."
    ),
)
async def post_console_command(
    payload: dict,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(
            status_code=403,
            detail="Insufficient role: operator or admin required for console commands",
        )
    action = str(payload.get("action", "")).lower()
    if action not in CONSOLE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown console action '{action}'")
    ctx.mqtt_service.publish_console_command(payload)
    return {"published": True, "action": action}


@router.post(
    "/direct/relay",
    summary="Direct relay control",
    description=(
        "Set a relay channel directly by number, bypassing config-based name resolution. "
        "Requires operator or admin role. state: 0=off, 1=on."
    ),
)
async def post_direct_relay(
    payload: DirectRelayPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_direct_relay(payload)
    return {"published_commands": commands}


@router.post(
    "/direct/servo",
    summary="Direct servo control",
    description=(
        "Set a servo/PWM channel directly by number and pulse width (µs), "
        "bypassing config-based name resolution. "
        "Requires operator or admin role. pulse_us=0 disables the output."
    ),
)
async def post_direct_servo(
    payload: DirectServoPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_direct_servo(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/buzzer",
    summary="Control the FMC buzzer",
    description=(
        "Drive the FAS FMC buzzer. action: begin (start a melody), note (append "
        "a freq/duration note), play (play melody by idx), stop (silence). "
        "Requires operator or admin role."
    ),
)
async def post_fas_buzzer(
    payload: FasBuzzerPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    try:
        commands = ctx.command_service.apply_fas_buzzer(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"published_commands": commands}


@router.post(
    "/fas/sd",
    summary="Control the FMC SD-card logger",
    description=(
        "Control the FAS FMC SD-card logger. action: set_rate (set the log "
        "decimation divisor, 1 = full rate) or clear (reformat the card). "
        "Requires operator or admin role."
    ),
)
async def post_fas_sd(
    payload: FasSdPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_sd(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/rab",
    summary="Recovery Arming Board arm/disarm",
    description=(
        "Arm or disarm a Recovery Arming Board (RAB). rab_id selects the unit "
        "(0 = A, 1 = B); the FMC momentarily pulses GPIO_ARM / GPIO_DISARM on the "
        "addressed RAB. Safety-critical: requires operator or admin role."
    ),
)
async def post_fas_rab(
    payload: FasRabPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    try:
        commands = ctx.command_service.apply_fas_rab(payload)
    except ValueError as exc:
        # Nova-lock blocks RAB arming (disarm is always allowed). 423 = Locked.
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    return {"published_commands": commands}


@router.post(
    "/fas/aux",
    summary="FMC auxiliary rail power (radio / RunCam / RF amplifier)",
    description=(
        "Switch an FMC auxiliary rail on or off. device: radio (the STM32WL "
        "vehicle modem, an FMC pin), runcam or rf_pa (EPB load switches the FMC "
        "is the single writer for). rfd is a deprecated alias for radio. "
        "Requires operator or admin role."
    ),
)
async def post_fas_aux(
    payload: FasAuxPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_aux(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/runcam_record",
    summary="Start or stop a RunCam recording",
    description=(
        "Start or stop a RunCam recording over the RunCam Device Protocol. This "
        "is distinct from powering the camera rail: with the firmware's "
        "rec_on_power default, bringing the rail up already starts a recording. "
        "autostop_s is the auto-stop timeout in seconds (0 = record until "
        "stopped, max 43200). Requires operator or admin role."
    ),
)
async def post_fas_runcam_record(
    payload: FasRuncamRecordPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_runcam_record(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/radio_config",
    summary="Write the FMC vehicle-radio configuration",
    description=(
        "Write (and persist) the complete STM32WL vehicle-radio configuration on "
        "the FMC, or request a read-back. Carried as one 88-byte bulk record, so "
        "it is valid only on the wired link — the firmware never accepts it over "
        "RF. The bridge validates the record against the firmware's own bounds "
        "and rejects a bad one with a logged reason. Requires admin role: this "
        "changes the licensed transmit parameters."
    ),
)
async def post_fas_radio_config(
    payload: FasRadioConfigPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.admin:
        raise HTTPException(status_code=403, detail="Insufficient role: admin required")
    commands = ctx.command_service.apply_fas_radio_config(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/sound",
    summary="Soundboard control (buzzer replacement)",
    description=(
        "Control the FMC soundboard. action: play (idx), stop, volume (0..255), "
        "tone (freq_hz/ms — the buzzer replacement), list (request the clip list), "
        "clear (erase all clips). Requires operator or admin role."
    ),
)
async def post_fas_sound(
    payload: FasSoundPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_sound(payload)
    return {"published_commands": commands}


@router.post(
    "/fas/charger",
    summary="PMB battery charging control",
    description=(
        "Enable or suspend PMB battery charging and optionally set the LTC4162 "
        "charge current/voltage limit DAC codes (0..31; omit to leave unchanged). "
        "Charging is default-OFF. Requires operator or admin role."
    ),
)
async def post_fas_charger(
    payload: FasChargerPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_charger(payload)
    return {"published_commands": commands}


def _soundboard_free_bytes(ctx: AppContext) -> int | None:
    """Free soundboard flash in bytes from the FMC's last reported status, or
    None when no soundboard telemetry has been seen."""
    flight = ctx.runtime.latest_flight_data or {}
    status = (flight.get("fas_sound") or {}).get("status") or {}
    cap_kb, used_kb = status.get("cap_kb"), status.get("used_kb")
    if not isinstance(cap_kb, (int, float)) or not cap_kb:
        return None
    used = used_kb if isinstance(used_kb, (int, float)) else 0
    return max(0, int((cap_kb - used) * 1024))


async def _read_upload(file: UploadFile) -> bytes:
    """Read an upload into memory, refusing anything past the source cap."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_SOURCE_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > SOUND_SOURCE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Source file exceeds {SOUND_SOURCE_MAX_BYTES} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/fas/sound/upload",
    summary="Upload a soundboard clip",
    description=(
        "Transcode an uploaded audio file (any format ffmpeg reads) to the FMC "
        "on-flash clip format, stage it for download, and tell the bridge to "
        "fetch and stream it to the soundboard. The request waits for the "
        "bridge's confirmation, so it can take a while for a long clip. "
        "format: 'pcm' (clean, 4x size) or 'adpcm' (compact, ~1/4 the flash). "
        "Clip size is bounded by the soundboard's free flash, not by MQTT. "
        "Requires operator or admin role; ffmpeg must be installed on the backend."
    ),
)
async def post_fas_sound_upload(
    request: Request,
    file: UploadFile = File(..., description="Audio file to transcode and upload"),
    name: str = Form(..., description="Clip name (<=24 chars on the FMC)"),
    format: str = Form("adpcm", description="'pcm' or 'adpcm'"),
    highpass_hz: int = Form(700, description="High-pass cutoff Hz (speaker shaping)"),
    pitch_semitones: float = Form(0.0, description="Pitch shift up in semitones (tempo kept)"),
    node: str | None = Form(None, description='FAS board node, default "FMC_0"'),
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    if not have_ffmpeg():
        raise HTTPException(status_code=503, detail="ffmpeg not installed on the backend host")

    raw = await _read_upload(file)
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")

    fmt = SND_FMT_IMA_ADPCM if str(format).lower() in ("adpcm", "ima_adpcm") else SND_FMT_PCM_S16
    try:
        # Transcoding is CPU/ffmpeg-bound and unbounded in length now — keep it
        # off the event loop so telemetry and other requests keep flowing.
        clip = await asyncio.to_thread(
            audio_to_clip, raw, highpass_hz=highpass_hz,
            pitch_semitones=pitch_semitones, fmt=fmt,
        )
    except TranscodeError as exc:
        raise HTTPException(status_code=422, detail=f"Transcode failed: {exc}") from exc

    data = clip["data"]
    if not data:
        raise HTTPException(status_code=422, detail="Transcode produced no audio")
    if len(data) > SOUND_CLIP_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"Clip is {len(data)} bytes (> {SOUND_CLIP_MAX_BYTES}). "
                    "Use a shorter clip or 'adpcm' format."),
        )
    free = _soundboard_free_bytes(ctx)
    if free is not None and len(data) > free:
        raise HTTPException(
            status_code=413,
            detail=(f"Clip is {len(data)} bytes but the soundboard has only {free} "
                    "bytes free. Clear clips or use a shorter clip / 'adpcm'."),
        )

    # The bridge may live on another host, so prefer an explicitly configured
    # public base URL over this request's (which could be localhost).
    base_url = os.getenv("NOVA_PUBLIC_BASE_URL") or str(request.base_url)
    result = await ctx.command_service.apply_fas_sound_upload(
        name=name[:24], clip=data, fmt=clip["format"], crc32=clip["crc32"],
        sample_rate=clip["sample_rate"], node=node, base_url=base_url,
    )
    payload = {"uploaded": {**result, "seconds": round(clip["seconds"], 2)}}
    if not result.get("ok"):
        status = 504 if result.get("stage") == "timeout" else 502
        raise HTTPException(status_code=status, detail=payload["uploaded"])
    return payload


@router.get(
    "/fas/sound/clip/{token}",
    summary="Download a staged soundboard clip",
    description=(
        "Serve the raw on-flash clip bytes for a pending upload. The FAS bridge "
        "fetches this URL after a /fas/sound/upload command; the token is "
        "single-use in practice (dropped once the bridge acknowledges) and "
        "expires on its own. Not intended for direct client use."
    ),
    response_class=FileResponse,
)
async def get_fas_sound_clip(token: str, ctx: AppContext = Depends(get_context)) -> FileResponse:
    clip = ctx.clip_store.get(token)
    if clip is None:
        raise HTTPException(status_code=404, detail="Unknown or expired clip token")
    return FileResponse(
        path=clip.path,
        media_type="application/octet-stream",
        filename=f"{clip.meta.get('name') or 'clip'}.bin",
        headers={"X-Clip-Crc32": str(clip.meta.get("crc32", "")),
                 "X-Clip-Bytes": str(clip.size)},
    )


@router.post(
    "/system-commands",
    summary="Send system command",
    description=(
        "Dispatch a Commands-section command. "
        "Role is checked via `X-Client-Id` header."
    ),
)
async def post_system_command(
    payload: SystemCommandPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    try:
        ctx.role_service.check_command_role(caller_role, payload.name, payload.state, ctx.config_service.config)
        return ctx.command_service.apply_system_command(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
