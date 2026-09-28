---
name: maya-anim-transfer
description: Retarget or author animation on a rigged character — HumanIK setup, mocap/BVH retarget, bake to control rig, cycles and loops, reach semantics, curve de-drift. Fire before keying anything on a character and before blaming a retarget. E.g. "pasa el mocap al rig", "retarget de este BVH", "hazle un ciclo de andar", "el loop da un salto", "la animación se ve rara".
---

# Animation transfer and authoring on a rigged character

Applies to **any** HumanIK-characterised rig, not one project's characters.
Everything here was falsified in-vivo. Most of these failures report success —
the viewport and the attribute read both agree with you while being wrong.

For *which* command or flag, ask `search_maya_docs` — it now carries a HumanIK
section (`CMDS_API.md`), so look there before assuming a command is missing. One
trap it records: HIK's `cmds` surface is **split**. `hikGlobals` plus twelve
UPPERCASE `HIK*` runtime commands handle pinning, body-part modes and keying;
characterisation and the control-rig bake are MEL via `mel.eval(...)`. Checking
with a lowercase `hik` grep finds one command and hides twelve.

What follows is the *procedure*, which the corpus does not hold.

## 1. Author on the control rig. Never key the deform skeleton

The deform skeleton is driven output. Keys placed there fight the rig, survive
bakes you did not intend, and produce a character that looks right in one frame
and broken in motion.

Author on the rig's controls (`*_Ctrl_*` or whatever the rig's convention is) and
let the deformation follow. **This cannot be enforced in code** — nothing can
reliably tell a deform joint from a control — so the discipline is yours.

Verify in **motion**, with a playblast. A correct-looking single frame proves
nothing about a deformation.

## 2. Rest pose is not zero

**Read the rest pose from the rig publish before you zero, copy or mirror
anything.** It is not 0, and it differs per character and per node.

"Zeroing a control" is therefore not "returning it to neutral" — it is moving it
to an arbitrary pose that happens to be numerically tidy. Every copy, mirror or
reset that assumed zero has to be redone.

## 3. Existing keys silently override `setAttr`

You `setAttr` a pose. You read the attribute back. It reads **0.0**, or the old
value, and you conclude the pose did not take.

It took — and then the existing animation curve overrode it on the next
evaluation. The attribute read is honest about the *result*, not about your
write.

**`cutKey` the channel first**, then pose. Otherwise you will spend an hour
debugging a write that was never the problem.

## 4. Auto Key and live reads

Scripts that touch a character must:

1. **Turn Auto Key off** before doing anything.
2. **Read the live attribute values BEFORE changing `currentTime`.**

Scrubbing the timeline destroys any pose the artist has not keyed. If you change
time first and read after, you are reading the evaluated curve, not the artist's
in-progress work — and you have already thrown that work away.

## 5. HumanIK reach semantics

`reach` decides who wins:

| reach | Winner |
|---|---|
| **1** | the effector — it commands the limb |
| **0** | FK — the effector is along for the ride |

Two things make this deceptive:

- **Effectors are born with reach OFF**, except hips and ankles. So the effector
  you just grabbed probably commands nothing.
- **Manipulating an effector always moves it regardless of reach.** The viewport
  shows it responding to you either way. You cannot tell reach state by dragging
  something — you must query it.

Also:

- **Full-Body key mode stamps everything.** One key becomes keys on the whole
  character. Use it when that is what you want, never as a default.
- **Guides are not effectors.** Authoring on a guide when you meant an effector
  produces motion that looks nearly right and bakes to nothing.

## 6. A bad retarget is usually a bad source

When a retarget comes out wrong, **measure the source curves against the raw file
before touching a single retarget setting.**

The canonical case: a BVH whose frame rate did not match the scene, stretching
the clip ×5. Every retarget parameter was innocent, and hours went into tuning
them. The evidence was in the source curves the whole time.

Order of diagnosis:

1. Compare the imported source curves to the raw file — duration, frame count,
   value range.
2. Only then look at characterisation and retarget settings.

`maya_import_file` handles BVH natively (rotate-order mapping is done for you,
240 s budget). That path is solid; suspect the file's frame rate before suspecting
the importer.

## 7. Bake in the right order

Retarget first, bake second, author third. Baking to the control rig **before**
the retarget is settled means authoring on top of motion you are about to
regenerate.

After a bake, re-verify §5 — bake changes what commands what.

Beware DG evaluation order and Euler/gimbal artefacts around the bake: a channel
that looked clean pre-bake can come out flipped. Check the curves, not the pose.

## 8. Cycles and loops: de-drift, then verify numerically

A loop that "looks fine" pops. For a cyclic channel, the requirement is exact:

```
value(start) == value(end)
```

**De-drift the curves so that holds, then verify it numerically.** Not by eye,
not by playblast — read the values. The eye cannot see a 0.3-unit pop at the loop
seam until it is on a screen in a room full of people.

Two more rules:

- **Do not force a cycle onto a non-cyclic gesture.** A wave or a glance should
  ping-pong, not wrap. Forcing a wrap produces a snap nothing can hide.
- **Measure translation on the thing that touches the ground** — the shoe or foot
  *mesh*, not the joint. Joint and contact surface differ by whatever the
  deformation does, and foot-slide is a property of the contact surface.

## 9. Calibrate the QA metric before showing anything

Before presenting a cycle, run the objective checks — and first **calibrate the
discriminating metric on a reference that was already approved**.

An uncalibrated metric produces confident numbers that do not separate good from
bad, and a metric that passes a known-bad clip is worse than no metric. Cloud-level
statistics in particular are blind to twist: symmetric-looking numbers over a set
of points say nothing about whether a limb is rotated wrong.

And: **never call a visual result resolved on the strength of a metric alone.**
Look at it.

## 10. Working on a live shared rig

- **Do not accumulate untracked stateful or destructive operations** on a rig
  someone else is using. If you cannot answer "what exactly did you change?", the
  work is not deliverable.
- For exploratory work, **use a throwaway copy**.
- **Stop thrashing.** After roughly two iterations that do not improve the result,
  stop and say so plainly. Hand-tuning animation blind past that point burns
  hours and usually makes it worse.

## What this skill is not

- **Not skinning or weights.** Bind bleed, weight transfer and mirroring by vertex
  correspondence are a separate problem from motion.
- **Not rig construction.** Joint placement, IK/FK setup and constraint authoring
  are not covered here.
- **Not batch processing.** Running a retarget across many files belongs in
  `maya-headless-batch` — and remember playblast verification is unavailable
  headless.
- **Not scene assembly.** Getting the character into the shot is
  `maya-scene-assembly`.
- **Not an API reference.** Command and flag lookup is `search_maya_docs` — which
  does not cover HumanIK; this file is the substitute.
