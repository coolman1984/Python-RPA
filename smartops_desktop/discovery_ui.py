from __future__ import annotations


def layer_summary(step):
    layers = ((step.get("discovery") or {}).get("layers") or {})
    if not layers:
        return "No discovery data", ""
    ok = []
    other = []
    for name, value in layers.items():
        status = value.get("status", "unknown") if isinstance(value, dict) else "unknown"
        label = name.replace("_", " ")
        if status == "ok":
            ok.append(label)
        else:
            other.append(f"{label}: {status}")
    headline = f"Seen {len(ok)}/{len(layers)}"
    detail = "Available: " + (", ".join(ok) if ok else "none")
    if other:
        detail += "\nOther: " + ", ".join(other)
    return headline, detail


def install(main_window_class):
    """Small UI integration kept separate from the recorder/engine layers."""
    if getattr(main_window_class, "__smartops_discovery_ui__", False):
        return
    main_window_class.__smartops_discovery_ui__ = True

    original_render = main_window_class.render_steps
    original_handle = main_window_class.handle_event

    def render_steps(self):
        original_render(self)
        if not self.current:
            return
        for row, step in enumerate(self.current.get("steps", [])):
            headline, detail = layer_summary(step)
            if detail and self.steps.item(row, 3):
                self.steps.item(row, 3).setText(headline)
                self.steps.item(row, 3).setToolTip(detail)
            for column in range(self.steps.columnCount()):
                item = self.steps.item(row, column)
                if item and detail:
                    item.setToolTip(detail)

    def handle_event(self, event):
        original_handle(self, event)
        if event.get("type") == "recorded":
            layers = event.get("layers") or []
            if layers:
                readable = ", ".join(str(x).replace("_", " ") for x in layers)
                self.log.insertPlainText(f"Discovery: {len(layers)} layers · {readable}\n")
            else:
                self.log.insertPlainText("Discovery: action saved; no enrichment layer confirmed.\n")
        elif event.get("type") == "discovery_observation":
            # The normal handler already writes the message to the activity log and run journal.
            pass

    main_window_class.render_steps = render_steps
    main_window_class.handle_event = handle_event
