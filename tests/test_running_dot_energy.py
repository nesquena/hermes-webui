"""Keep the activity pulse cheap without suppressing its running state."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_running_ring_pulse_does_not_animate_paint_properties():
    """The pulse lives on the ::after ring and animates only compositor-only
    properties (transform + opacity); the dot core itself stays solid."""
    css = (ROOT / 'static/style.css').read_text()
    # Anchor on the one-line @keyframes rule and inspect BOTH frames — a
    # first-frame-only split would miss paint properties in the 100% frame.
    ring_line = next(
        line for line in css.splitlines() if line.startswith('@keyframes wlring{')
    )
    assert 'transform:' in ring_line
    assert 'opacity:' in ring_line
    assert 'box-shadow:' not in ring_line
    assert 'filter:' not in ring_line
    # The dot CORE rule (not a skin override — skin blocks embed the same
    # selector with an ancestor prefix) carries no animation, so the core
    # keeps full-opacity contrast in every theme.
    core = css.split('\n.tool-card-running-dot{', 1)[1].split('}', 1)[0]
    assert 'animation:' not in core
    assert 'background:var(--accent);' in core


def test_running_dot_honors_reduced_motion():
    css = (ROOT / 'static/style.css').read_text()
    assert (
        '@media (prefers-reduced-motion:reduce){\n'
        '  .tool-card-running-dot::after,.tl-rundot::after{animation:none;opacity:0;}\n'
        '}' in css
    )
