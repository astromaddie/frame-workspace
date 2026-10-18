# FW-12 input progress

Keyboard and trackpad input for the native Workspace stays on the Mac. Native
controller pointer input remains disabled; this record does not claim worn
multi-panel focus or Japanese IME acceptance.

## Corrected viewer boundary behavior

The browser viewer previously sent wheel events even when the pointer was in a
letterbox or pillarbox region. The Mac then clamped those coordinates to the
nearest edge of the captured source and scrolled there. Wheel input now follows
the picture bounds already used for pointer-down. Pointer-up remains deliverable
outside the picture so leaving the panel during a drag cannot leave a button
held. Pointer cancellation still sends the session release message.

`tests/page/macview_input.mjs` executes the actual production input block with
canvas geometry and captures its outgoing messages. It covers a 16:9 picture in
a taller viewport with a nonzero page origin, a portrait picture in a wide
viewport, line/page wheel units, and outside drag release. The test is included
in `python3 -m unittest discover -s tests -p test_input.py` (39 tests passed).
This verifies browser coordinate/event behavior, not macOS event delivery.

## Mapping and focus audit

The browser maps contain-fitted picture coordinates to normalized coordinates.
The Mac maps those into `capture.frameRect` in global Mac points. Display crops
offset their source rectangle into display-local coordinates while retaining
the crop's global origin for input. Separated sources use their virtual-display
bounds. No flipped or scaled-coordinate defect was found in this source audit.

Plain-window focus raises and activates the selected window before injecting
its click. Display and separated-display clicks rely on global event hit-testing;
their focus messages do not select an individual app window. The inherited
window focus implementation uses `_AXUIElementGetWindow` for AX identity mapping;
this work adds no private Spaces manipulation or secure-input bypass.

## Remaining acceptance checklist

- Verify physical and virtual display corners, negative display origins, and
  crop boundaries using owned test windows; check clicks, selection drag, scroll
  inertia and pointer release after disconnect.
- Check Mac keyboard focus after switching between ordinary-window, separated
  display and native full-screen sources, including inactive Spaces once the
  FW-06 gate is available.
- Check English and Japanese IME composition, modifiers, app switching, popovers,
  system dialogs and secure-input restrictions in a worn session.

These items remain open. VM input tests, source inspection and successful
capture do not substitute for the real-device checklist.
