from pathlib import Path


EXTENSION = Path("/Users/parantoux/Andy/workspace/hermes-webui/extensions/project-os/project-os-extension.js")
TESTS = Path("/Users/parantoux/Andy/workspace/hermes-webui/tests/test_project_os_extension_regressions.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"missing expected snippet for {label}")
    return text.replace(old, new, 1)


extension = EXTENSION.read_text(encoding="utf-8")
extension = replace_once(
    extension,
    'async function resolveAuthoritativeControlPlaneBoard(currentBoardSlug = "") {\n'
    '    const knownBoards = Array.isArray(state.boards) ? state.boards : [];\n'
    '    const boardExists = (boardSlug) => knownBoards.some((board) => board.slug === boardSlug);\n'
    '    const canonicalCurrentBoard = String(currentBoardSlug || "").trim();\n'
    '    const localCurrentBoard = String(state.currentBoard || "").trim();\n'
    '    const sessionCandidates = [state.projectSession, { session_id: state.projectSessionId }, { session_id: state.submit?.sessionId }]\n',
    'async function resolveAuthoritativeControlPlaneBoard(currentBoardSlug = "", options = {}) {\n'
    '    const knownBoards = Array.isArray(state.boards) ? state.boards : [];\n'
    '    const boardExists = (boardSlug) => knownBoards.some((board) => board.slug === boardSlug);\n'
    '    const canonicalCurrentBoard = String(currentBoardSlug || "").trim();\n'
    '    const localCurrentBoard = String(state.currentBoard || "").trim();\n'
    '    const allowSessionCandidates = options.allowLinkedSessionBoard !== false;\n'
    '    const sessionCandidates = [state.projectSession, { session_id: state.projectSessionId }, { session_id: state.submit?.sessionId }]\n',
    "resolveAuthoritative signature",
)
extension = replace_once(
    extension,
    '      .map((entry) => ({\n'
    '        session_id: String(entry?.session_id || "").trim(),\n'
    '        session: entry?.title ? entry : null,\n'
    '      }))\n'
    '      .filter((entry, index, array) => entry.session_id && array.findIndex((candidate) => candidate.session_id === entry.session_id) === index);\n'
    '\n'
    '    for (const candidate of sessionCandidates) {\n',
    '      .map((entry) => ({\n'
    '        session_id: String(entry?.session_id || "").trim(),\n'
    '        session: entry?.title ? entry : null,\n'
    '      }))\n'
    '      .filter((entry, index, array) => entry.session_id && array.findIndex((candidate) => candidate.session_id === entry.session_id) === index);\n'
    '\n'
    '    if (allowSessionCandidates) {\n'
    '      for (const candidate of sessionCandidates) {\n',
    "session candidate guard open",
)
extension = replace_once(
    extension,
    '      state.projectSession = session;\n'
    '      state.projectSessionId = String(session?.session_id || candidate.session_id || "").trim();\n'
    '      return boardSlug;\n'
    '    }\n'
    '\n'
    '    if (canonicalCurrentBoard && boardExists(canonicalCurrentBoard)) {\n',
    '        state.projectSession = session;\n'
    '        state.projectSessionId = String(session?.session_id || candidate.session_id || "").trim();\n'
    '        return boardSlug;\n'
    '      }\n'
    '    }\n'
    '\n'
    '    if (canonicalCurrentBoard && boardExists(canonicalCurrentBoard)) {\n',
    "session candidate guard close",
)
extension = replace_once(
    extension,
    'async function prepareControlPlaneBoardContext() {\n'
    '    const boardsPayload = await api("/api/kanban/boards");\n'
    '    state.boards = boardsPayload.boards || [];\n'
    '    const authoritativeBoardSlug = await resolveAuthoritativeControlPlaneBoard(boardsPayload.current);\n',
    'async function prepareControlPlaneBoardContext(options = {}) {\n'
    '    const boardsPayload = await api("/api/kanban/boards");\n'
    '    state.boards = boardsPayload.boards || [];\n'
    '    const authoritativeBoardSlug = await resolveAuthoritativeControlPlaneBoard(boardsPayload.current, options);\n',
    "prepareControlPlaneBoardContext signature",
)
extension = replace_once(
    extension,
    '        const controlPlaneBoard = await prepareControlPlaneBoardContext();\n',
    '        const controlPlaneBoard = await prepareControlPlaneBoardContext({ allowLinkedSessionBoard: false });\n',
    "dispatch prepareControlPlaneBoardContext call",
)
EXTENSION.write_text(extension, encoding="utf-8")

tests = TESTS.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    '    assert \'async function resolveAuthoritativeControlPlaneBoard(currentBoardSlug = "") {\' in EXTENSION_JS\n',
    '    assert \'async function resolveAuthoritativeControlPlaneBoard(currentBoardSlug = "", options = {}) {\' in EXTENSION_JS\n',
    "tests resolve signature",
)
tests = replace_once(
    tests,
    "    assert 'const localCurrentBoard = String(state.currentBoard || \"\").trim();' in EXTENSION_JS\n",
    "    assert 'const localCurrentBoard = String(state.currentBoard || \"\").trim();' in EXTENSION_JS\n"
    "    assert 'const allowSessionCandidates = options.allowLinkedSessionBoard !== false;' in EXTENSION_JS\n",
    "tests allowSessionCandidates assert",
)
tests = replace_once(
    tests,
    '    assert \'async function prepareControlPlaneBoardContext() {\' in EXTENSION_JS\n',
    '    assert \'async function prepareControlPlaneBoardContext(options = {}) {\' in EXTENSION_JS\n',
    "tests prepareControlPlaneBoardContext signature",
)
tests = replace_once(
    tests,
    '    assert \'const authoritativeBoardSlug = await resolveAuthoritativeControlPlaneBoard(boardsPayload.current);\' in EXTENSION_JS\n',
    '    assert \'const authoritativeBoardSlug = await resolveAuthoritativeControlPlaneBoard(boardsPayload.current, options);\' in EXTENSION_JS\n',
    "tests resolve call in prepare",
)
tests = replace_once(
    tests,
    "    assert 'const controlPlaneBoard = await prepareControlPlaneBoardContext();' in dispatch\n",
    "    assert 'const controlPlaneBoard = await prepareControlPlaneBoardContext({ allowLinkedSessionBoard: false });' in dispatch\n",
    "tests dispatch call",
)
TESTS.write_text(tests, encoding="utf-8")

print("patched ER-030 candidate")
