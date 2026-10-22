# Setting up a BowlerDepot kiosk (GMKtec G10 + touchscreen)

This turns a GMKtec G10 mini PC and a touchscreen into a locked-down kiosk. It
starts straight into **https://kiosk.bowlerdepot.com**, full screen, and
customers can't get out of it. Plan on about 45 minutes per unit. Runbook 6ei
has the background.

**You'll need:**
- the G10, the touchscreen (HDMI for video **and** USB for touch), and power
  for both;
- a **USB keyboard and mouse** for setup. Afterwards, keep the keyboard
  somewhere staff can find it: it's how you get out of kiosk mode;
- internet, preferably an Ethernet cable;
- the **location name** for this unit, e.g. `Aurora`.

---

## 1. Check Windows

1. Connect everything and turn it on. Finish Windows setup with an
   **admin account** you'll keep, e.g. `BowlerDepot Admin`, and give it a
   password you store somewhere safe.
2. Open **Settings → System → About** and look at **Windows specifications →
   Edition**. It must say **Windows 11 Pro**: kiosk mode isn't available on
   Windows 11 Home. If it says Home, stop here and tell Claude.
3. **Settings → Windows Update → Check for updates.** Install everything and
   restart until there's nothing left. Do the same in Edge (**⋯ → Settings →
   About Microsoft Edge**). It's easier to update now than once it's locked.

## 2. Make it turn itself back on after a power cut

This is a BIOS setting. The wording varies by BIOS version.

1. Restart, and keep tapping **Delete** (or **F7**/**Esc** on some units) while
   it starts, until the blue/grey BIOS screen appears.
2. Look under **Advanced**, **Chipset** or **Power** for a setting named
   **Restore on AC Power Loss**, **State After G3**, **AC Power Loss** or
   **Power On After Power Fail**.
3. Set it to **Power On** (sometimes shown as **S0** or **Always On**).
4. **Save & Exit** (usually F10). If you can't find it, skip this step and
   tell Claude the BIOS menu names you do see.

## 3. Never sleep

1. **Settings → System → Power** (or **Power & battery**) → **Screen, sleep &
   hibernate timeouts**. Set **turn off my screen** and **put my device to
   sleep** to **Never**.
2. **Settings → Windows Update → Advanced options → Active hours** → set it to
   cover store hours (e.g. 8 AM – 11 PM). Windows then only restarts for
   updates overnight.

## 4. Check the touchscreen

Tap around the screen. If touches land in the wrong place, or touch only works
on one screen, open **Control Panel → Hardware and Sound → Tablet PC Settings →
Setup…**, choose **Touch input** and follow the prompts to match touch to this
screen.

## 5. Apply the lockdown settings

These lock Edge to the kiosk site and turn off the touchscreen's edge swipes.

1. Click **Start**, type **PowerShell**, right-click **Windows PowerShell →
   Run as administrator**, and click **Yes**.
2. Paste these two lines and press Enter:

   ```powershell
   Invoke-WebRequest https://kiosk.bowlerdepot.com/setup/kiosk-lockdown.ps1 -OutFile $env:TEMP\kiosk-lockdown.ps1
   powershell -ExecutionPolicy Bypass -File $env:TEMP\kiosk-lockdown.ps1
   ```

3. It should end with **Kiosk lockdown applied** in green. Restart the PC.

To undo it later, run the same thing with `kiosk-unlock.ps1` instead of
`kiosk-lockdown.ps1`.

## 6. Turn on kiosk mode

1. Sign in with the admin account. Go to **Settings → Accounts → Other
   users**, find **Kiosk**, and click **Set up a kiosk → Get started**.
2. **Account name:** `Kiosk`. Click **Next**.
3. **Choose the app:** **Microsoft Edge**. Click **Next**.
4. **How will this kiosk be used?** Choose **As a digital sign or
   interactive display**. Don't choose "public browser": it would wipe the
   kiosk's saved settings every session.
5. **URL:** the kiosk address with this unit's location on the end:

   ```
   https://kiosk.bowlerdepot.com/?location=Aurora
   ```

   Use the real location name. For two words, use `%20` for the space, e.g.
   `?location=Main%20St`, or just a hyphen, `?location=Main-St`.
6. Click **Next**, then **Close**.
7. **Restart.** The PC signs in to the kiosk account by itself and opens the
   kiosk full screen. From now on, this is all it does when it starts.

## 7. Test it

Go through each of these on the touchscreen.

- [ ] **Starts up:** after a restart it lands on the BowlerDepot attract
      screen with no sign-in, and the location shows in the top-right corner.
- [ ] **Navigation:** touch to start, open **Find my ball**, a ball page and
      **Browse**. Back and Home work.
- [ ] **Videos:** on a ball page, play a review video. Then tap the video's
      title and the YouTube logo: **nothing should open**.
- [ ] **QR code:** scan a ball's "take it with you" code with your phone. It
      opens the ball's page on `kiosk.bowlerdepot.com/m/…`.
- [ ] **No way out:** swipe in from every screen edge; nothing should
      appear. Long-press in different places; no menus.
- [ ] **Idle reset:** leave it on a ball page for 2 minutes. It returns to the
      attract screen.
- [ ] **Power cut:** pull the PC's power for 10 seconds, then plug it back
      in. It turns on and comes back to the kiosk by itself.
- [ ] **Settings screen:** hold the logo for 3 seconds and enter PIN
      **2468**. You should see the location and this unit's device ID. Tap
      **Done**.

## Day-to-day

- **Getting out of kiosk mode** (to update or fix something): plug in the
  keyboard and press **Ctrl + Alt + Delete**. Choose **Sign out**, then sign
  in with the admin account.
- **Changing the location:** sign in as admin, then **Settings → Accounts →
  Other users → Kiosk** and change the URL's `?location=`. The kiosk's own
  settings screen can't override a location set in the URL; it says so if
  you try.
- **Updates to the kiosk app** need nothing on the PC. The site updates
  itself; the next restart (or the settings screen's **Reload app**) picks
  them up.
- **Optional nightly restart** keeps a long-running browser fresh. Run this
  once in an admin PowerShell:

  ```powershell
  schtasks /create /tn "Kiosk nightly restart" /tr "shutdown /r /t 0" /sc daily /st 04:00 /ru SYSTEM
  ```

## If something's wrong

| Problem | Try |
|---|---|
| "Set up a kiosk" isn't there | The PC is on Windows 11 Home (step 1). |
| It opens Edge with an address bar | Step 6 used "public browser". Remove the kiosk and set it up again with "digital sign or interactive display". |
| "This site has been blocked" | A kiosk address is mistyped in step 6 (must start `https://kiosk.bowlerdepot.com`). Or rerun step 5. |
| Review videos won't play | Tell Claude. The YouTube player may need one more allowed address. |
| Touch is offset or not working | Step 4, and check the touchscreen's USB cable is connected to the PC. |
| Doesn't come back after a power cut | Step 2's BIOS setting. |
| Location missing in the corner | The URL in step 6 has no `?location=`, or it's misspelled. |
