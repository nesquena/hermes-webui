"""Keep the activity pulse cheap without suppressing its running state."""
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_running_ring_pulse_does_not_animate_paint_properties():
    """The pulse lives on the ::after ring and animates only compositor-only
    properties (transform + opacity); the dot core itself stays solid."""
    css = (ROOT / 'static/style.css').read_text()
    ring = css.split('@keyframes wlring{', 1)[1].split('}', 1)[0]
    assert 'transform:' in ring
    assert 'opacity:' in ring
    assert 'box-shadow:' not in ring
    assert 'filter:' not in ring
    # The dot core carries no animation — full-opacity contrast in every theme.
    dot_block = css.split('.tool-card-running-dot{', 1)[1].split('}', 1)[0]
    assert 'animation:' not in dot_block
    assert 'background:var(--accent);' in dot_block


def test_running_dot_honors_reduced_motion():
    css = (ROOT / 'static/style.css').read_text()
    assert (
        '@media (prefers-reduced-motion:reduce){\n'
        '  .tool-card-running-dot::after,.tl-rundot::after{animation:none;opacity:0;}\n'
        '}' in css
    )
