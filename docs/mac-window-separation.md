# Real separation of Mac windows in the headset

Research and experiments on 2026-09-28 (macOS 26.5.2, Apple M5 Pro), for
making each streamed Mac window truly independent. Builds on
[mac-in-headset.md](mac-in-headset.md). Labels: **verified** (tried here),
**documented**, **source** (read in someone's code) or **reported**.

## What "separate" lacks today

Today each window is captured on its own
(`SCContentFilter(desktopIndependentWindow:)`), but it still lives on the
Mac's one desktop:

- **Clicks.** A click has to raise the window first, so it reorders the
  Mac's windows, steals focus and moves the real cursor.
- **Child windows.** A window's menus, popovers, sheets and tooltips are
  separate windows, so they aren't in its panel. Apple documents that the
  single-window filter leaves them out.
- **Size.** A panel's size is tied to the window's size on the Mac screen.
- **Hidden windows.** Minimised windows, and windows on other Spaces, can't
  be shown live.

## How the Mac draws, and where pixels can be read

Apps draw with Core Animation into IOSurfaces. WindowServer's compositor
stacks every window onto each display, including virtual ones, and sends
the result to the screen. Pixels can be read at three points:

| Where | How | Gets | Doesn't get |
|---|---|---|---|
| One window's content | ScreenCaptureKit `desktopIndependentWindow` (public, what we use) | Live, zero-copy, even when covered by other windows (**documented**) | Menus, popovers, sheets. It pauses while the window is minimised (**reported**) |
| One window plus its children | `SCStreamConfiguration.includeChildWindows` (macOS 14.2+, **reported**) | Menus, popovers and sheets attached to the window | Anything the app puts outside the window's bounds |
| A whole display | ScreenCaptureKit display filter, with apps or windows included or excluded | Everything on that display, cursor included | Only what's on that display |
| A snapshot of any window | Private `CGSHWCaptureWindowList`, which AltTab uses for minimised windows and other Spaces (**source**: `alt-tab-macos` `PrivateApis.swift`) | Minimised windows and other Spaces | It's one still image, not a live stream |
| Another app's layer tree | Private `CALayerHost` with a context id | A live, zero-copy picture | It only works when the other app cooperates, so it's no good for arbitrary windows (**reported**) |

`CGWindowListCreateImage` and `CGDisplayStream` are obsolete in the macOS 15
SDK (**reported** by MacPorts and JUCE). Nothing reads another app's pixels
without the Screen Recording permission.

## The idea: each window gets its own virtual display

A virtual display (the private `CGVirtualDisplay`, as used by BetterDisplay,
DeskPad and quest-display) is a compositor target with no physical screen.
Put one streamed window alone on its own virtual display, sized to its
panel, and capture the whole display:

- **Nothing can overlap it.** A plain click at that point always lands on
  that window, so input doesn't need raising or background-event tricks.
- **Menus, sheets, popovers, tooltips and context menus appear on the same
  display, so they're in the panel.** With "Displays have separate Spaces"
  on (it is on this Mac), each display also has its own menu bar, so the
  app's menu bar can be part of the panel.
- **The panel's size is the display's size,** in HiDPI. Resizing the panel
  means changing the display mode and resizing the window to fill it
  (Accessibility API).
- **Minimised windows and other Spaces stop being a problem,** because
  streamed windows live on their own displays.
- **The cursor goes where the laser points.** That display's panel is the
  one being used, much as Vision Pro's Mac Virtual Display works. Keys from
  the Mac's keyboard go to the window last clicked.

### Verified here (no permission needed)

- **Creating one.** It needs no permission or entitlement. 1920×1080 HiDPI
  gives a 3840×2160-pixel, 60 Hz display, placed next to the built-in
  screen, and `NSScreen.screensHaveSeparateSpaces` was true.
- **Many at once.** 16 were created at once with no error.
- **Placement.** `CGConfigureDisplayOrigin` far away failed with error
  1014: macOS keeps displays edge to edge. Disabling one with the private
  `CGSConfigureDisplayEnabled` from a command-line tool also failed with 1014.
- **Removal is deferred while the physical screen sleeps.** With the
  built-in display asleep, virtual displays were not removed when released,
  or when their process exited, even from their own `.app`. A second display
  with the same vendor, product and serial couldn't be created. All of them
  disappeared as soon as the screen woke (`caffeinate -u`). The helper must
  therefore:
  - keep a fixed pool of displays and reuse them rather than create new ones;
  - keep the screen awake while they exist, which it already does while
    streaming.

### Built: "Give each window its own display"

Frame Control's helper now does this (`mac/frame-mac-view/Sources/Separate.swift`),
and it's the default in the Tools card. Streams of this kind are named
`separate:<window id>`.

- For each window shown, it creates a HiDPI virtual display. The display is
  sized to the window plus a menu bar, with a fresh serial each time, so a
  display left over while the screen slept can't block a new one.
- The window is moved onto the display and resized to fill it (Accessibility
  API), and the whole display is captured.
- On **Stop** the window goes back where it was, and the display is released.
- Plain per-window capture is still there: untick "Give each window its own
  display". Separate mode needs Accessibility (to move the window), and
  without it, Show says so rather than quietly falling back.

Verified 2026-09-28 (macOS 26.5.2, M5 Pro), using a TextEdit test document
and the benchmark's Chrome windows:

- **Separation works.** The window moved onto its own display and streamed,
  with its first frames in 3.8 s. The display showed TextEdit's own menu bar.
- **Child windows come along.** A context menu and the Page Setup sheet
  opened on the same display and were in the capture.
- **Input.** Typing through the stream reached the window. The benchmark's
  typing scenario does this on every run.
- **Stop.** Stop put the window back at exactly its old size (656×422), and
  the display was removed.
- **The helper must run a real Cocoa event loop.** Earlier, it ran only a
  `RunLoop`. With that, AppKit never learned about new displays:
  - `NSScreen.screens` never listed them, so placing the window always waited
    3 s and then gave up;
  - the display's HiDPI mode never applied, so windows were captured at 1×
    (1280×860 instead of 1920×1290 pixels) and text was soft.

  Running `NSApplication` (with no Dock icon) fixed both. The screen now
  appears within 100 ms, and captures are at 2× (**verified** in
  `bench/results/*-baseline.json` against `*-baseline-fixed.json`).
- **Frame rate.** Chrome drew at 60–61 fps on the virtual display, but
  ScreenCaptureKit delivered only about 46–53 fps from it, whereas the
  built-in ProMotion screen gave 121 fps (**verified**). Neither a 120 Hz
  virtual display (`FRAME_MAC_VIEW_VD_HZ=120`) nor a looser
  `minimumFrameInterval` changed that. Cause unknown. (The full-display
  Workspace capture behaved differently on 2026-10-19: a 2 ms interval
  allowance plus a 120 Hz display took it from about 54.6 to 60–62 fps; see
  [the 60 fps record](frame-workspace/PERFORMANCE.md#60-fps-single-workspace-2026-10-19).)
- **Windows that keep their own size** (Calculator, 230×408). The window
  moved and streamed, but stayed small in the display's corner, so the panel
  was mostly wallpaper. Now only that corner is captured
  (`SCStreamConfiguration.sourceRect`): the menu bar and the window, at least
  480×360 points so menus fit. Clicks map to the cropped area. The first
  frame arrived in 0.6–1.1 s, and on Stop the window went back and the
  display was removed. A display's menu bar names the active app, so it
  shows the window's own menus only once the panel has been clicked.
- **Several windows at once** (on the Mac, with a local stand-in viewer that
  acknowledges frames). Three and four Chrome windows, each scrolling on its
  own display at 1920×1290 pixels, streamed at 53–56 fps and about
  9 Mbit/s each. The helper used 20% (three) or 24% (four) of one CPU core.
  The hardware encoder is shared: its time per frame went from 6.7 ms for
  one stream to 7–15 ms for three and 11–25 ms for four, so each extra
  moving window adds latency to the others. Once in four runs, before a fix,
  one window left its display when several displays appeared at the same
  moment, and its stream stopped: nothing on that display was changing any
  more. The helper now checks every second and puts the window back. Three
  further runs with four windows were clean, and every display was gone
  afterwards (checked with `CGGetOnlineDisplayList`).
- **Quitting.** On SIGTERM, or when Frame Control goes away, the helper ends
  every stream first, so windows go back before their displays disappear.
  Verified: a 900×600 window was back at 900×600 after SIGTERM. Closing a
  viewer 0.05–1.5 s into startup left the window at its old size and no
  extra display.
- **A consent prompt.** macOS 26 asks whether to let ScreenCaptureKit apps
  "bypass the system private window picker". The prompt appeared *on the
  virtual display*, so it would show up inside the headset panel. Allow it
  once on the Mac.

### Still to check

1. That a context menu opens over the text being clicked, not just somewhere
   on the display. This needs a retest after the consent prompt above is
   allowed.
2. Stage Manager, which is reported to undo Accessibility resizes.
3. Where the Dock and the cursor go on a virtual display.
4. Closing the lid with virtual displays attached (clamshell).
5. Many moving windows at once in the headset: whether to lower the bitrate
   or frame rate of panels you aren't using, so the one you are using keeps
   the encoder to itself.
6. All of it in the headset, with the laser.

## Input without disturbing the Mac (a finer option)

For windows that stay on the Mac's own screen, input can go to a window
behind others without raising it:

- **Background click.** `CGEventPostToPid` with the CGEvent fields 91 and
  92, which say which window a click is for (`kCGMouseEventWindowUnderMousePointer`
  and `...ThatCanHandleThisEvent`), plus a window-relative location
  (**reported**, reverse-engineered, needs testing).
- **Focus without raise.** yabai makes a window key without raising it by
  posting event records with the private `SLPSPostEventRecordTo` (**source**:
  `yabai/src/window_manager.c`).
- **Known failures.**
  - Chromium and Electron ignore background clicks unless they're built
    with the 2026 `acceptsFirstMouse` fix (electron/electron#54493).
  - Games and canvas apps often need real activation.
  - Password fields (Secure Event Input) drop synthetic keys.
  - IME composition, as for Chinese or Japanese input, isn't reliable.

With a virtual display per window, most of this isn't needed. It's worth
having for hovering over one panel while typing in another.

## Encoding and transport

- **Codec.** Keep H.264 4:2:0. Apple's High Performance Screen Sharing uses
  4:4:4 over two virtual displays (**documented**), but the Frame's Chromium
  decodes H.264 in software, and no 4:4:4 decode path is confirmed there.
- **Sharper static text.** When a panel has been still for a moment, send a
  high-quality refresh, either a keyframe at low quantisation or a lossless
  WebP overlay, so static text is sharp. Moving content stays as video.
- **Transport.** Keep WebSocket over SSH for now, as it works everywhere.
  WebRTC (UDP, congestion control) is the proven step up for Wi-Fi.
  WebTransport over QUIC is promising, but its server side on macOS is
  unverified.
