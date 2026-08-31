"""
voice_scenarios — what to ask the voice loop, and what counts as an answer.
==========================================================================

Each scenario drives the real socket from `voice_drive` and checks one claim from
the spec. They are deliberately written as *behavioural* claims rather than
assertions about internals: "the pause did not end the turn" rather than
"`turn_complete_probability` was 0.24". The kernel's unit tests already pin the
numbers; what is unproven until something drives the whole stack is whether those
numbers reach the socket at all — which is exactly the class of defect the audit
kept finding (a complete, tested kernel with nothing wired to it).

Where a scenario cannot prove something, it says so with `report.note` instead of
quietly passing. There is no microphone in this loop, so nothing here is evidence
about speech recognition accuracy or how the voice sounds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict

from .voice_drive import Report, connect, paint


def header(title: str, spec: str) -> None:
    print()
    print(paint(f"── {title} ", "bold") + paint(f"({spec})", "dim"))


#: Anything in this list reaching a TTS engine means the user hears the model's
#: internals read aloud. This is the check `model_output` was written for.
LEAKS = ("<think>", "<|tool_call", "<|channel|>", "thinking process", "**", "<|im_")


async def scenario_reply(report: Report, opts: argparse.Namespace) -> None:
    """Does it answer a question like a person, with a real model behind it?"""
    header("Conversational reply", "§1 §2 §19 — real OpenRouter call")
    async with connect("drive-reply") as v:
        v.mark()
        await v.say("Hey Akansha, what is the capital of France?")
        # Returns as soon as it speaks instead of always burning the full budget,
        # then keeps a short tail so the rest of a multi-sentence reply lands.
        if await v.wait_for_speech(20.0):
            await v.wait(1.5)

        spoken = [s for s in v.said() if s]
        report.check("it said something at all", bool(spoken), f"{len(spoken)} utterance(s)")
        joined = " ".join(spoken)
        report.check("the answer is on topic", "paris" in joined.lower(),
                     joined[:70] or "nothing spoken")

        leaked = [m for m in LEAKS if m.lower() in joined.lower()]
        report.check("no scaffolding or markdown reached the voice", not leaked,
                     f"leaked: {leaked}" if leaked else "clean")
        latency = v.time_to_first_speech()
        if latency is not None:
            report.note(
                "time to first spoken word",
                f"{latency:.1f}s from end of utterance, including endpointing",
            )



async def scenario_pause(report: Report, opts: argparse.Namespace) -> None:
    """§6's canonical example, driven the way a recogniser actually delivers it."""
    header("Mid-sentence pause", "§6 — 'Open my project and… find the failing tests'")
    async with connect("drive-pause") as v:
        print(f"   {paint('MIC  USER', 'blue')}  \"Open my project and\" …then stops talking")
        await v.send("speech_start")
        for part in ("Open", "Open my", "Open my project", "Open my project and"):
            await v.send("partial_transcript", text=part, confidence=0.9)
            await asyncio.sleep(0.05)
        await v.send("speech_end")

        # Longer than the 1200ms the client used to hardcode: the old timer fired
        # right here and cut the sentence in half.
        await v.wait(1.8)
        premature = [k for k in v.kinds()
                     if k in ("generate_response", "execute_plan", "finalize_turn")]
        report.check("the pause did not end the turn", not premature,
                     f"fired: {premature}" if premature else "turn held open past 1.8s")

        print(f"   {paint('MIC  USER', 'blue')}  …\"find the failing tests\"")
        whole = "Open my project and find the failing tests"
        await v.send("speech_start")
        await v.send("partial_transcript", text=whole, confidence=0.93)
        await v.send("speech_end")
        await v.send("final_transcript", text=whole, confidence=0.93)
        await v.wait(3.0)
        acted = [k for k in v.kinds()
                 if k in ("generate_response", "execute_plan", "finalize_turn")]
        report.check("the completed sentence did end the turn", bool(acted),
                     f"fired: {sorted(set(acted))}")


async def scenario_backchannel(report: Report, opts: argparse.Namespace) -> None:
    """§7 — 'yeah' is not a task."""
    header("Backchannels", "§7 — 'yeah' / 'okay' / 'mm-hmm' must not become tasks")
    async with connect("drive-backchannel") as v:
        for word in ("yeah", "okay", "mm-hmm", "right"):
            v.clear()
            await v.say(word)
            await v.wait(1.6)
            bad = [k for k in v.kinds() if k in ("generate_response", "execute_plan")]
            report.check(f"'{word}' produced no task and no answer", not bad,
                         f"fired: {bad}" if bad else "listened")


async def scenario_bargein(report: Report, opts: argparse.Namespace) -> None:
    """§4 — cutting in must stop the audio and abandon the superseded answer."""
    header("Barge-in", "§4 §20 — interrupt while speaking, no Stop button")
    async with connect("drive-bargein") as v:
        await v.say("Tell me a long story about the history of the Roman empire.")
        await v.wait(6.0)
        await v.send("tts_started", text="Long ago in the city of Rome")
        await asyncio.sleep(0.3)

        v.clear()
        print(f"   {paint('MIC  USER', 'blue')}  (cuts in) \"stop\"")
        await v.send("speech_start")
        await v.send("partial_transcript", text="stop", confidence=0.9)
        await v.wait(1.2)

        kinds = set(v.kinds())
        report.check("it stopped speaking when cut off",
                     bool(kinds & {"stop_tts", "cancel_execution"}),
                     f"got: {sorted(kinds)[:6]}")


async def scenario_correction(report: Report, opts: argparse.Namespace) -> None:
    """§35 — a correction amends the plan instead of restarting the conversation."""
    header("Correction", "§35 — 'Deploy to production' → 'Actually, staging'")
    async with connect("drive-correction") as v:
        await v.say("Deploy the app to production")
        await v.wait(5.0)
        v.clear()
        await v.say("Actually, staging")
        await v.wait(6.0)

        kinds = set(v.kinds())
        report.check("the correction was acted on, not ignored",
                     bool(kinds & {"execute_plan", "generate_response",
                                   "ask_clarification", "request_confirmation"}),
                     f"got: {sorted(kinds)[:6]}")
        spoken = " ".join(v.said()).lower()
        if spoken:
            report.check("it did not simply re-announce production",
                         "production" not in spoken or "staging" in spoken, spoken[:70])


async def scenario_concurrent(report: Report, opts: argparse.Namespace) -> None:
    """§14 — answering 'what are you doing?' must not destroy the task."""
    header("Talk while executing", "§14 — a question mid-task keeps the task")
    async with connect("drive-concurrent") as v:
        await v.send("task_started", description="reorganise the downloads folder")
        await v.send("step_started", node_id="n1", description="listing files")
        before = (await v.tick()).get("task_active")

        v.clear()
        await v.say("What are you doing right now?")
        await v.wait(8.0)
        after = (await v.tick()).get("task_active")

        report.check("the task was active before the question", before is True,
                     f"task_active={before}")
        report.check("the question did not cancel the task", after is True,
                     f"task_active={after}")
        report.check("it answered while working", bool([s for s in v.said() if s]),
                     " ".join(v.said())[:70] or "said nothing")


async def scenario_memory(report: Report, opts: argparse.Namespace) -> None:
    """§11 §13 — does the second turn know what the first one said?"""
    header("Memory across turns", "§11 §13 — context carried, not rebuilt")
    async with connect("drive-memory") as v:
        await v.say("Remember that my project folder is called nightingale.")
        await v.wait(16.0)
        v.clear()
        await v.say("What did I just say my project folder is called?")
        await v.wait(20.0)

        answer = " ".join(v.said()).lower()
        report.check("it recalled the fact from the previous turn",
                     "nightingale" in answer, answer[:90] or "said nothing")
        tick = v.last_tick()
        if tick:
            report.note("context meter",
                        f"{tick.get('context_indicator')} fill={tick.get('context_fill')}")


async def scenario_automation(report: Report, opts: argparse.Namespace) -> None:
    """§16 §40 — the automation bridge. Plans by default; executes only on --desktop."""
    header("Automation", "§16 §40 — "
           + ("REAL desktop control" if opts.desktop else "planning only (no --desktop)"))
    if not opts.desktop:
        from ..main import build_browser_prompt_plan

        for prompt in ("open youtube and search for lo-fi beats",
                       "open notepad and type hello"):
            plan = build_browser_prompt_plan(prompt)
            steps = plan.get("steps") or []
            print(f"   {paint('PLAN', 'magenta')}      {prompt}")
            for step in steps[:5]:
                print(f"            {paint('->', 'dim')} {step.get('action')} "
                      f"{paint(str(step.get('target'))[:52], 'dim')}")
            report.check(f"a plan was produced for {prompt!r}",
                         bool(steps) or bool(plan.get("needs_clarification")),
                         f"{len(steps)} step(s)"
                         + (" / needs clarification" if plan.get("needs_clarification") else ""))
        report.note("desktop execution",
                    "skipped — pass --desktop to let it drive the real mouse")
        return

    async with connect("drive-automation") as v:
        await v.say("Open notepad")
        await v.wait(40.0)
        kinds = set(v.kinds())
        report.check("an execution directive was issued", "execute_plan" in kinds,
                     f"got: {sorted(kinds)[:6]}")
        report.note("steps reported",
                    ", ".join(f.get("directive", "") for f in v.frames
                              if f.get("directive") in ("execute_plan", "speak"))[:100])


async def scenario_resume(report: Report, opts: argparse.Namespace) -> None:
    """§37 — a dropped socket is not the end of the task."""
    header("Reconnect and resume", "§37 §38 — disconnect mid-task, come back")
    sid = "drive-resume"
    async with connect(sid) as v:
        await v.send("task_started", description="index the whole repository")
        await v.send("step_started", node_id="r1", description="walking the tree")
        active = (await v.tick()).get("task_active")
        report.check("a task was running before the drop", active is True,
                     f"task_active={active}")
        # Leave *without* session_close — a browser tab closing looks like this,
        # and the kernel must tell the two apart (§34).
        v.detach()

    await asyncio.sleep(0.8)
    async with connect(sid) as v2:
        ready = next((f for f in v2.frames if f.get("directive") == "session_ready"), {})
        resume = next((f for f in v2.frames if f.get("directive") == "resume_available"), None)
        report.check("reconnecting found the same session", ready.get("reconnected") is True,
                     f"reconnected={ready.get('reconnected')}")
        report.check("the unfinished task was offered back", resume is not None,
                     json.dumps(resume)[:90] if resume else "no resume_available frame")


async def scenario_telugu(report: Report, opts: argparse.Namespace) -> None:
    """The user's own requirement: English and Telugu both understood."""
    header("Telugu", "§26 — 'english , telugu both understanding'")
    async with connect("drive-telugu", language="te") as v:
        await v.say("నా ప్రాజెక్ట్ ఫోల్డర్ ఏమిటి చెప్పు")
        await v.wait(18.0)
        acted = {"generate_response", "execute_plan", "ask_clarification", "speak"}
        report.check("a Telugu utterance was interpreted, not dropped",
                     bool(set(v.kinds()) & acted), f"got: {sorted(set(v.kinds()))[:6]}")

        v.clear()
        # Romanised Telugu — how people actually type and speak it.
        await v.say("chrome open cheyyi")
        await v.wait(8.0)
        report.check("romanised Telugu was interpreted too",
                     bool(set(v.kinds()) & acted), f"got: {sorted(set(v.kinds()))[:6]}")


async def scenario_tts(report: Report, opts: argparse.Namespace) -> None:
    """Real audio to a real file. Proves the voice renders, not that it is pleasing."""
    header("Voice rendering", "§26 — real edge-tts audio")
    from ..main import generate_edge_tts_audio

    try:
        audio = await generate_edge_tts_audio(
            "Hello, I'm Akansha. I've opened your project and found three failing tests.",
            "female", "friendly", "en",
        )
    except Exception as exc:
        report.check("edge-tts rendered audio", False, f"{type(exc).__name__}: {exc}")
        return

    report.check("edge-tts rendered audio", len(audio) > 2000, f"{len(audio):,} bytes")
    out = Path(__file__).with_name("_voice_drive_sample.mp3")
    out.write_bytes(audio)
    report.note("sample written", str(out))
    report.note("what this does not prove",
                "no microphone or speaker in this loop — the audio renders, but "
                "lip-sync and perceived voice quality are not measured here")


SCENARIOS: Dict[str, Callable[[Report, argparse.Namespace], Any]] = {
    "reply": scenario_reply,
    "pause": scenario_pause,
    "backchannel": scenario_backchannel,
    "bargein": scenario_bargein,
    "correction": scenario_correction,
    "concurrent": scenario_concurrent,
    "memory": scenario_memory,
    "automation": scenario_automation,
    "resume": scenario_resume,
    "telugu": scenario_telugu,
    "tts": scenario_tts,
}

#: Scenarios that need a live model. Skipped under --no-llm so the wiring half
#: stays testable when OpenRouter is out of credit — which is how it was found.
NEEDS_LLM = {"reply", "correction", "concurrent", "memory", "telugu"}
