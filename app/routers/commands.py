from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile

from app.context import AppContext
from app.deps import get_context
from app.models import (
    CommandPayload,
    DirectRelayPayload,
    DirectServoPayload,
    FasAuxPayload,
    FasBuzzerPayload,
    FasChargerPayload,
    FasRabPayload,
    FasRfPayload,
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

# Cap the transcoded clip so a base64 clip in one MQTT command stays sane.
SOUND_UPLOAD_MAX_BYTES = 400_000

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
        "configure, tx. Requires operator or admin role (raw frame TX is "
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
    if action not in {"start", "stop", "list_ports", "configure", "tx"}:
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
    summary="FMC auxiliary power (RFD / RunCam)",
    description=(
        "Switch an FMC auxiliary load switch on or off. device: rfd (RFD900x "
        "radio) or runcam. Requires operator or admin role."
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
    "/fas/rf",
    summary="Set FMC RF telemetry rate/power mode",
    description=(
        "Set the FMC RF telemetry rate/power mode: 0 = low (default, power-saving), "
        "1 = normal, 2 = high. The mode is persisted on the FMC — only send this on "
        "an explicit operator change. Requires operator or admin role."
    ),
)
async def post_fas_rf(
    payload: FasRfPayload,
    x_client_id: str | None = Header(default=None),
    ctx: AppContext = Depends(get_context),
) -> dict:
    caller_role = ctx.role_service.resolve_caller_role(x_client_id)
    if caller_role < ClientRole.operator:
        raise HTTPException(status_code=403, detail="Insufficient role: operator or admin required")
    commands = ctx.command_service.apply_fas_rf(payload)
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


@router.post(
    "/fas/sound/upload",
    summary="Upload a soundboard clip",
    description=(
        "Transcode an uploaded audio file (any format ffmpeg reads) to the FMC "
        "on-flash clip format and stream it to the soundboard via the bridge. "
        "format: 'pcm' (clean, 4x size) or 'adpcm' (compact, better over MQTT). "
        "Requires operator or admin role; ffmpeg must be installed on the backend."
    ),
)
async def post_fas_sound_upload(
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

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")

    fmt = SND_FMT_IMA_ADPCM if str(format).lower() in ("adpcm", "ima_adpcm") else SND_FMT_PCM_S16
    try:
        clip = audio_to_clip(raw, highpass_hz=highpass_hz,
                             pitch_semitones=pitch_semitones, fmt=fmt)
    except TranscodeError as exc:
        raise HTTPException(status_code=422, detail=f"Transcode failed: {exc}") from exc

    data = clip["data"]
    if not data:
        raise HTTPException(status_code=422, detail="Transcode produced no audio")
    if len(data) > SOUND_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"Clip is {len(data)} bytes (> {SOUND_UPLOAD_MAX_BYTES}). "
                    "Use a shorter clip or 'adpcm' format."),
        )

    summary = ctx.command_service.apply_fas_sound_upload(
        name=name[:24], clip=data, fmt=clip["format"], crc32=clip["crc32"],
        sample_rate=clip["sample_rate"], node=node,
    )
    return {"uploaded": {**summary, "seconds": round(clip["seconds"], 2)}}


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
