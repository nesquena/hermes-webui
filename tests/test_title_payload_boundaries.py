"""Worker regressions from the independent rebased-title audit."""
import json

import pytest

from tests import test_title_review_matrix as matrix

run_worker = matrix.run_worker
worker_scene = matrix.worker_scene


LEAKS = [
    pytest.param('Title - ["Rejected Candidate", "Other"]', id='dash-label'),
    pytest.param('## Title: ["Rejected Candidate", "Other"]', id='heading-label'),
    pytest.param('> Title: ["Rejected Candidate", "Other"]', id='quote-label'),
    pytest.param('Title: ```json\n["Rejected Candidate", "Other"]\n```', id='label-fence'),
    pytest.param('Suggested title: ["Rejected Candidate", "Other"]', id='suggested-label'),
    pytest.param('> ## Title: ["Rejected Candidate", "Other"]', id='nested-label-markers'),
    pytest.param('`Title:` ["Rejected Candidate", "Other"]', id='code-label'),
    pytest.param('Title: [1, true]', id='numeric-label-array'),
    pytest.param(r'Output: {"t\u0069tle": "Rejected Candidate"}', id='escaped-key'),
    pytest.param(r'Title - {"t\u0069tle": "Rejected Candidate"}', id='escaped-key-dash'),
    pytest.param(json.dumps({'title': json.dumps(json.dumps(['Rejected Candidate', 'Other']))}),
                 id='nested-string-array'),
    pytest.param(json.dumps({'title': json.dumps(' ["Rejected Candidate", "Other"] ')}),
                 id='nested-string-array-whitespace'),
    pytest.param('Title: [' + '[' * 2000 + '"Rejected Candidate"' + ']' * 2000 + ']',
                 id='deep-label-array'),
    pytest.param('Possibly I should call it Rejected Candidate', id='possibly'),
    pytest.param('Probably I should call it Rejected Candidate', id='probably'),
    pytest.param('How about Rejected Candidate', id='how-about'),
    pytest.param('What about Rejected Candidate', id='what-about'),
    pytest.param('I think Rejected Candidate', id='i-think'),
    pytest.param("I'd suggest Rejected Candidate", id='suggest'),
    pytest.param('Maybe I think Rejected Candidate', id='hedged-think'),
    pytest.param("Probably I'd suggest Rejected Candidate", id='hedged-suggest'),
    pytest.param('- Maybe I should call it Rejected Candidate', id='bullet-hedge'),
    pytest.param('> Maybe I should call it Rejected Candidate', id='quote-hedge'),
    # Any short label, not only known ones, bounds the payload after it.
    pytest.param('Title — ["Rejected Candidate", "Other"]', id='em-dash-label'),
    pytest.param('Title – ["Rejected Candidate", "Other"]', id='en-dash-label'),
    pytest.param('1. Title: ["Rejected Candidate", "Other"]', id='numbered-label'),
    pytest.param('* Result: ["Rejected Candidate", "Other"]', id='bullet-other-label'),
    pytest.param('Result: ["Rejected Candidate", "Other"]', id='other-label'),
    pytest.param('Answer: {"name": "Rejected Candidate"}', id='other-label-object'),
    pytest.param("Here's my pick: [\"Rejected Candidate\", \"Other\"]", id='phrase-label'),
    pytest.param('Title: ~~~json\n["Rejected Candidate", "Other"]\n~~~', id='tilde-fence'),
    pytest.param('I guess Rejected Candidate', id='i-guess'),
    pytest.param('Perhaps, I think, Rejected Candidate', id='hedge-comma-chain'),
    # A string-bearing container that ends the reply is the payload, whatever
    # prefix (long phrase, odd separator, HTML, emoji, zero-width) precedes it.
    pytest.param('Here is my chosen output: ["Rejected Candidate", "Other"]', id='long-label'),
    pytest.param('Result => ["Rejected Candidate", "Other"]', id='arrow-label'),
    pytest.param('Result:: ["Rejected Candidate", "Other"]', id='double-colon-label'),
    pytest.param('Result | ["Rejected Candidate", "Other"]', id='pipe-label'),
    pytest.param('Result： ["Rejected Candidate", "Other"]', id='fullwidth-colon-label'),
    pytest.param('<b>Result:</b> ["Rejected Candidate", "Other"]', id='html-label'),
    pytest.param('\U0001f3f7️ Result: ["Rejected Candidate", "Other"]', id='emoji-label'),
    pytest.param('​Title: ["Rejected Candidate", "Other"]', id='zero-width-label'),
    pytest.param('<b>I guess</b> Rejected Candidate', id='html-hedge'),
    pytest.param('​Maybe I should call it Rejected Candidate', id='zero-width-hedge'),
    # A string-bearing container right after any separator is the payload,
    # even with a trailing remark.
    pytest.param('Result => ["Rejected Candidate", "Other"] hope this helps', id='arrow-suffix'),
    pytest.param('Here is my chosen output: ["Rejected Candidate", "Other"] enjoy',
                 id='long-label-suffix'),
    pytest.param('Result | ["Rejected Candidate"] (two options)', id='pipe-suffix'),
    # Filler before an opinion lead, and more opinion leads.
    pytest.param('Well, I think Rejected Candidate', id='filler-i-think'),
    pytest.param('So I guess Rejected Candidate', id='filler-i-guess'),
    pytest.param('Hmm, maybe I should call it Rejected Candidate', id='filler-hedge'),
    pytest.param('Okay so I think Rejected Candidate', id='filler-chain'),
    pytest.param('I believe Rejected Candidate', id='i-believe'),
    pytest.param("Let's go with Rejected Candidate", id='lets-go-with'),
    pytest.param('My pick is Rejected Candidate', id='my-pick'),
    # Multi-line lists are menus, whatever heading introduces them.
    pytest.param('Titles:\n- "Rejected Candidate"\n- "Other"', id='bullet-menu'),
    pytest.param('Suggestions:\n1. Rejected Candidate\n2. Other', id='numbered-menu'),
    pytest.param('Here are two:\n* Rejected Candidate\n* Other', id='star-menu'),
    # HTML entities, block tags and format characters are presentation only.
    pytest.param('Result: [&quot;Rejected Candidate&quot;, &quot;Other&quot;]', id='entity-payload'),
    pytest.param('I&#32;guess Rejected Candidate', id='entity-hedge'),
    pytest.param('<p>I guess</p> Rejected Candidate', id='paragraph-hedge'),
    pytest.param('<div>I guess Rejected Candidate</div>', id='div-hedge'),
    pytest.param('I⁦ guess Rejected Candidate', id='bidi-format-hedge'),
    pytest.param('Maybe well I think Rejected Candidate', id='hedge-filler-opinion'),
    pytest.param('> - Maybe I should call it Rejected Candidate', id='nested-hedge-markers'),
]


@pytest.mark.parametrize('route', ('aux', 'agent'))
@pytest.mark.parametrize('refresh', (False, True))
@pytest.mark.parametrize('content', LEAKS)
def test_payload_boundary_recovers(worker_scene, monkeypatch, route, refresh, content):
    session, saved, calls, events = run_worker(
        worker_scene, monkeypatch, route, refresh, [content, 'Valid Recovery Title'])
    assert session.title == saved['title'] == 'Valid Recovery Title'
    assert saved['llm_title_generated'] is True
    assert len(calls) == 2
    assert [data['title'] for name, data in events if name == 'title'] == ['Valid Recovery Title']
    assert sum(name == 'stream_end' for name, _ in events) == (0 if refresh else 1)


@pytest.mark.parametrize('route', ('aux', 'agent'))
@pytest.mark.parametrize('refresh', (False, True))
@pytest.mark.parametrize('content', LEAKS)
def test_persistent_payload_is_not_published(worker_scene, monkeypatch, route, refresh, content):
    session, saved, calls, events = run_worker(
        worker_scene, monkeypatch, route, refresh, [content])
    published = [data['title'] for name, data in events if name == 'title']
    if refresh:
        assert not published
        assert session.title == saved['title'] == 'Existing Title'
        assert saved['llm_title_generated'] is True
    elif saved is None:
        assert not published
        assert session.title == 'Untitled'
        assert session.llm_title_generated is False
    else:
        # Preserve the existing local-summary fallback for invalid JSON;
        # reasoning-invalid replies instead leave the provisional unresolved.
        assert session.title == saved['title'] == 'Fix login'
        assert saved['llm_title_generated'] is True
        assert published == ['Fix login']
    assert len(calls) >= 2
    assert sum(name == 'stream_end' for name, _ in events) == (0 if refresh else 1)


@pytest.mark.parametrize('route', ('aux', 'agent'))
@pytest.mark.parametrize('refresh', (False, True))
@pytest.mark.parametrize('schema', (False, True))
@pytest.mark.parametrize('title', (
    'Title - Login Repair', '## Title: Login Repair', '> Title: Login Repair',
    'Suggested title: Login Repair', 'Output: Login Repair',
    '> ## Title: Login Repair', 'Title - [1, 2] Parsing Tutorial',
    'Possibly Maybe Album Review', 'Probably Approximately Correct Learning',
    'How About That Podcast', 'What About Bob Film Review', 'I Think Therefore I Am',
    'Maybe I Think Therefore I Am',
    'I’d Suggest Alternatives: Writing Guide', 'Maybe I’ll Be There Lyrics',
    'Maybe Use APIs In Python', '[1, 2] Parsing Tutorial', 'Parse {"a": 1} Payload Example',
    'Output: [1, 2, 3] Explained', 'Title-Case Converter Guide', 'Title IX Compliance Guide',
    'Result: Login Repair Notes', 'Answer: 42 Explained', 'Re: Weekly Sync Notes',
    "I Guess That's Why They Call It The Blues", 'Use ["a", "b"] As Default List',
    'Inspect {}', 'Compare [1, 2]', 'Notes - [Draft] Review', 'Bug: [object Object] In UI',
    'Release: [1.2.3] Notes', 'Chapter 3 - The Return', 'Step 1: Install Node',
    # Non-string literals after a label are subjects, not payloads.
    'JSON: {}', 'Changelog: [1, 2, 3]', 'Flags: [true, false]', 'Defaults: [null]',
    'Änderungen: [1, 2]',
    # Mixed-case brand words count as capitalized.
    'How About iPhone Repair', 'I Guess eBPF Tracing', 'Maybe iOS Widgets Guide',
    # Literal containers mid-title remain prose.
    'Explain {"ok": true} Responses', '<b> Tag Styling Basics',
    'Well Water Testing Guide', 'So I Married An Axe Murderer Review',
    'I Believe I Can Fly Lyrics', "Let's Go With The Flow Essay", 'My Pick Is Python Podcast',
    'Okay Computer Album Review', 'Hmm Sound Effects Library', 'Sorting [3, 1, 2] In Python',
    'Pipes | Unix Basics', 'Arrow => Functions In JS', '1. Getting Started Guide',
    # Only the opinion words decide casing; code literals after them don't.
    "Let's Use pytest Fixtures", "Let's Use npm Workspaces", 'Well, How About pytest Fixtures',
    # Code-tagged text is a literal mention, like backticks.
    '<code>I guess</code> Grammar Guide',
    'My Favorite Things Lyrics', 'I Picked A Winner Podcast', 'Titles And Subtitles In Film',
    'Chapter 1 - Part 2 Recap', 'The Best Title Is Short Essay', 'Maybe So Long Lyrics',
    'AT&amp;T Billing Help', 'Fish &amp; Chips Recipe',
    # Round-11 audit subjects that master keeps.
    '<code>the user is</code> Grammar Guide', 'HTML &quot;the user is&quot; Escaping',
    'When the user is Offline', 'Handling Requests When the user needs Help',
    'Why the best title is Short', 'Git Flags: - n - v Explained',
    'Python 1. Setup 2. Testing Guide', 'Steps 1. Install 2. Configure',
    'I love ["a"] Python Lists', 'My list ["a"] Tutorial', 'Our defaults ["a"] Explained',
    'We use ["a"] As Defaults', 'Understanding {"ok": true}', 'Here is ["a"] Syntax Explained',
    '<b>I Guess</b> eBPF Tracing', 'So You Think You Can Dance', 'Well Known TCP Ports',
    'Hmm Haskell Type Inference', "Let's Use eBPF Tracing", 'My Pick Is iPhone Repair',
    'The Best Title Is Short Essay',
    # Round-13 audit subjects that master keeps: capitalized JSON literals,
    # lowercase terms after a capitalized lead, explanatory clauses and
    # colon-introduced steps or flags.
    'Compare ["React", "Vue"] Performance', 'Enum ["Red", "Green"] Basics',
    'Parse {"framework": "React"} Responses', 'Translate {"welcome": "Hello"} Messages',
    'Compare ["HTTP 2", "HTTP 3"] Performance', 'Compare <code>["React", "Vue"]</code> Performance',
    'Use ["Login Repair"] As Example', 'I Guess eBPF on linux', "Let's Use npm with pnpm",
    'We use ["a"] as Defaults', 'Understanding why the user is offline',
    "Understanding 'the user is' in grammar", 'Tutorial: 1. Install 2. Configure',
    'Git Flags: - verbose - quiet Explained',
    # Round-14 audit subjects that master keeps.
    'Good title is a Subject Complement', 'understanding why the user is offline',
    "understanding 'the user is' in grammar",
    # Round-15 audit subjects that master keeps.
    '<code>The best title is</code> Grammar Guide', '&quot;The best title is&quot; Grammar Guide',
    '“The best title is” Grammar Guide', 'The best title is a Subject Complement',
    'The final title would be a noun phrase', '- "Summer" Memoir Review',
))
def test_boundary_subjects_survive(worker_scene, monkeypatch, route, refresh, schema, title):
    content = json.dumps({'title': title}) if schema else title
    session, saved, calls, events = run_worker(
        worker_scene, monkeypatch, route, refresh, [content])
    assert session.title == saved['title'] == title
    assert saved['llm_title_generated'] is True
    assert len(calls) == 1
    assert [data['title'] for name, data in events if name == 'title'] == [title]
