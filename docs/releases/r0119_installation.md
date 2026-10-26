# r0119 installation and verification

**This is candidate guidance only.** It does not authorize publication, nor a
KOGR or WRJE installation. Each station needs its own explicit operator
authorization and its own Update Center action. Nothing here is implied
approval for either station.

## A maintenance window is required

Installing r0119 restarts the audio engine:
- any Remote DJ session ends;
- playout is briefly interrupted and then automatically resumes.

There is no seamless live Remote DJ continuity across this upgrade.

## Preconditions (each station, before installing)

1. **Confirm the station and its installed release.** The live checkout must be
   exactly the r0118 release commit `3cbe9ce4bc1e134040a3d9571b3ac6523cbb535f`
   on `main`, with a clean worktree and generation-15 protected runtime.
   - A station still on r0117 (`ad52af0e97b0f7242dc4f91f0bca886cf69780be`)
     plans r0118 and r0119 together. That includes r0118's manual migration,
     covered by r0118's companion. Review that plan separately; do not assume
     it is the same as an r0118 station's.
2. **Confirm the trusted target.** It must be the reviewed, published r0119 tip,
   and the r0119 manifest must resolve to its single release-introducing
   commit.
3. **Confirm no Remote DJ is connected.** Stop rather than displacing a live
   session.
4. **Confirm the timing is acceptable.** Ending the engine process and briefly
   interrupting playout must be operationally acceptable now (not during a live
   show, a voice-track sequence about to air, or other critical programming).
5. **Review the Update Center preflight.** The plan from r0118 must show:
   - releases `[r0119]`;
   - no migrations and no manual operations;
   - no companion dependency;
   - no collectstatic, package, unit, daemon-reload, nginx or protected-runtime
     action;
   - restarts exactly `isadoraair-gunicorn`, then `isadoraair-engine`.

   Stop if anything else appears.
6. **Keep the normal safeguards.** Preserve the normal checkpoint and the
   protected updater's rollback/recovery rules. Make no ad-hoc repair of the
   station's Git state.

## Install

Use the normal Update Center workflow against the reviewed trusted tip.
Required restart order:

1. `isadoraair-gunicorn`
2. `isadoraair-engine`

Between the two restarts, updated Django may be serving while the old engine
still runs. Remote PTT then fails closed in **both** directions: the old engine
publishes no PTT generation, so the endpoint refuses every Remote PTT request,
OFF as well as ON. Old pages are refused too, because they send no generation.
A connected DJ could therefore not switch their microphone off through the
Console during this window. That is why precondition 3 (no Remote DJ
connected) is a safety requirement, not a convenience. The window closes when
the engine restarts, which ends any session anyway.

## After installation

1. **Health.**
   - The Update Center job succeeded.
   - The station's HEAD is the r0119 release commit.
   - `manage.py check` is clean, apart from the known pre-existing `security.W004`
     HSTS deployment warning.
   - `systemctl is-active isadoraair-gunicorn isadoraair-engine` reports both
     active.
   - The engine journal shows a clean start and auto-resume of playout.
2. **Reload every open Console tab and every open `/remote-dj/` tab fully.**
   Pages opened before the upgrade keep the r0118 code and cannot operate
   Remote PTT until reloaded. This is intentional fail-closed behaviour: their
   requests lack the PTT generation.
3. **Establish a new Remote DJ session** from a reloaded `/remote-dj/` page.
4. **Verify the generation-aware contract.**
   - With the DJ connected, the owner's Remote PTT button is enabled and the
     status block carries a PTT generation.
   - In a controlled silent or off-air recovery test, an accepted owner OFF
     remains effective if recovery advanced the generation before the browser
     observed it: the microphone stays OFF after reconnection. Perform this
     only under the same safe-audio conditions as the ON/OFF test below.
   - **The PTT ON/OFF test puts the test DJ's microphone on air.** Do it only
     when the operator has deliberately arranged that:
     - a silent or muted input at the test browser;
     - a scheduled break or test window;
     - or an off-air path the station uses for tests.

     Otherwise verify only the read-only items in this step and the
     administrator Disable/Enable items in step 5, which can only force the
     microphone OFF.
5. **Verify the consolidated buttons:**
   - **The owning browser,** even an administrator's, shows ordinary PTT and
     Disconnect, never Disable/Enable or Kick.
   - **An authorized administrator** in another browser sees Remote PTT as
     Disable/Enable and the Connect button as Kick Remote DJ:
     - Disable forces PTT DISABLED;
     - the DJ's button then reads PTT DISABLED and cannot open the mic;
     - Enable leaves the microphone OFF until the DJ presses PTT again;
     - Kick asks for confirmation naming the DJ and session, and dismissing it
       changes nothing.
   - **A user without the emergency grants** sees read-only indicators only.
   - **The separate Emergency Mute, Release and Kick buttons** are absent.
6. **Verify ordinary playback and engine health:** decks, queue, Start Next,
   Studio Mic and Auto/Manual behave as before.

Record each station separately: the installed release, exact trusted tip,
restart results and verification output.
