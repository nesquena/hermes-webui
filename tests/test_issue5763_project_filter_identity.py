"""Executable regression coverage for profile-qualified sidebar project filters."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(f"node failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    return json.loads(result.stdout.strip())


def test_same_project_id_in_different_profiles_filters_sessions_hidden_rows_and_references():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const src = fs.readFileSync({str(SESSIONS_JS)!r}, 'utf8');

        function extractFunction(name) {{
          const marker = `function ${{name}}(`;
          const start = src.indexOf(marker);
          if (start < 0) throw new Error(name + ' not found');
          const brace = src.indexOf('{{', start);
          let depth = 0;
          for (let i = brace; i < src.length; i++) {{
            if (src[i] === '{{') depth += 1;
            else if (src[i] === '}}') {{
              depth -= 1;
              if (depth === 0) return src.slice(start, i + 1);
            }}
          }}
          throw new Error('unterminated function ' + name);
        }}

        globalThis.NO_PROJECT_FILTER = '__none__';
        globalThis.S = {{
          activeProfile: 'default',
          activeProfileIsDefault: false,
          rootProfileNames: ['default'],
          session: null,
        }};
        globalThis._activeProject = {{ profile: 'work', project_id: 'shared' }};
        globalThis._showArchived = true;
        globalThis._sessionSourceFilter = 'webui';
        globalThis.window = {{ _showCliSessions: false }};
        globalThis._archivedCliCount = 0;
        globalThis._archivedWebuiCount = 0;
        globalThis._sidebarRowHasVisibleMessages = () => true;
        globalThis._isCliSession = () => false;
        globalThis._sidebarReferenceSessions = [
          {{ session_id: 'ref-work', profile: 'work', project_id: 'shared' }},
          {{ session_id: 'ref-other', profile: 'other', project_id: 'shared' }},
          {{ session_id: 'ref-unassigned', profile: 'work', project_id: '' }},
        ];

        for (const name of [
          '_canonicalProjectFilterProfile',
          '_projectFilterIdentity',
          '_projectFilterMatches',
          '_partitionSidebarSessionRows',
          '_scopedSidebarReferenceRows',
        ]) eval(extractFunction(name));

        const rows = [
          {{ session_id: 'work-visible', profile: 'work', project_id: 'shared', default_hidden: false, archived: false }},
          {{ session_id: 'work-hidden', profile: 'work', project_id: 'shared', default_hidden: true, archived: false }},
          {{ session_id: 'other-visible', profile: 'other', project_id: 'shared', default_hidden: false, archived: false }},
          {{ session_id: 'other-hidden', profile: 'other', project_id: 'shared', default_hidden: true, archived: false }},
          {{ session_id: 'other-project', profile: 'work', project_id: 'else', default_hidden: false, archived: false }},
        ];

        const partition = _partitionSidebarSessionRows(rows, null);
        const scoped = _scopedSidebarReferenceRows(false);
        console.log(JSON.stringify({{
          sessionsRaw: partition.sessionsRaw.map((row) => row.session_id),
          referenceRaw: partition.webuiReferenceRaw.map((row) => row.session_id),
          scopedRefs: scoped.map((row) => row.session_id),
        }}));
        """
    )
    assert _run_node(script) == {
        "sessionsRaw": ["work-visible", "work-hidden"],
        "referenceRaw": ["work-visible", "work-hidden"],
        "scopedRefs": ["ref-work"],
    }


def test_same_project_id_uses_matching_profile_metadata_and_delete_identity():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const src = fs.readFileSync({str(SESSIONS_JS)!r}, 'utf8');

        function extractFunction(name) {{
          const marker = `function ${{name}}(`;
          const start = src.indexOf(marker);
          if (start < 0) throw new Error(name + ' not found');
          const brace = src.indexOf('{{', start);
          let depth = 0;
          for (let i = brace; i < src.length; i++) {{
            if (src[i] === '{{') depth += 1;
            else if (src[i] === '}}') {{
              depth -= 1;
              if (depth === 0) return src.slice(start, i + 1);
            }}
          }}
          throw new Error('unterminated function ' + name);
        }}

        globalThis.NO_PROJECT_FILTER = '__none__';
        globalThis.S = {{ activeProfile: 'default', activeProfileIsDefault: false, rootProfileNames: ['default'] }};
        for (const name of ['_canonicalProjectFilterProfile', '_projectFilterIdentity', '_projectFilterMatches']) {{
          eval(extractFunction(name));
        }}

        const projects = [
          {{ profile: 'work', project_id: 'shared', color: '#123456', name: 'Work project' }},
          {{ profile: 'other', project_id: 'shared', color: '#abcdef', name: 'Other project' }},
          {{ project_id: 'shared', color: '#fedcba', name: 'Default project' }},
        ];
        const workSession = {{ profile: 'work', project_id: 'shared' }};
        const defaultSession = {{ project_id: 'shared' }};
        const activeFilter = {{ profile: 'work', project_id: 'shared' }};

        const workProject = projects.find((p) => _projectFilterMatches(_projectFilterIdentity(workSession), p));
        const defaultProject = projects.find((p) => _projectFilterMatches(_projectFilterIdentity(defaultSession), p));
        const deleteMatches = projects.map((p) => _projectFilterMatches(activeFilter, p));

        console.log(JSON.stringify({{
          workProject,
          defaultProject,
          deleteMatches,
        }}));
        """
    )
    assert _run_node(script) == {
        "workProject": {
            "profile": "work",
            "project_id": "shared",
            "color": "#123456",
            "name": "Work project",
        },
        "defaultProject": {
            "project_id": "shared",
            "color": "#fedcba",
            "name": "Default project",
        },
        "deleteMatches": [True, False, False],
    }


def test_root_alias_matches_default_profile_rows():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const src = fs.readFileSync({str(SESSIONS_JS)!r}, 'utf8');

        function extractFunction(name) {{
          const marker = `function ${{name}}(`;
          const start = src.indexOf(marker);
          if (start < 0) throw new Error(name + ' not found');
          const brace = src.indexOf('{{', start);
          let depth = 0;
          for (let i = brace; i < src.length; i++) {{
            if (src[i] === '{{') depth += 1;
            else if (src[i] === '}}') {{
              depth -= 1;
              if (depth === 0) return src.slice(start, i + 1);
            }}
          }}
          throw new Error('unterminated function ' + name);
        }}

        globalThis.NO_PROJECT_FILTER = '__none__';
        globalThis.S = {{
          activeProfile: 'kinni',
          activeProfileIsDefault: true,
          rootProfileNames: ['default', 'kinni'],
        }};
        for (const name of ['_canonicalProjectFilterProfile', '_projectFilterIdentity', '_projectFilterMatches']) {{
          eval(extractFunction(name));
        }}

        const session = {{ profile: 'kinni', project_id: 'shared' }};
        const project = {{ profile: 'default', project_id: 'shared' }};
        console.log(JSON.stringify({{
          identity: _projectFilterIdentity(session),
          matches: _projectFilterMatches(_projectFilterIdentity(session), project),
        }}));
        """
    )
    assert _run_node(script) == {
        "identity": {"profile": "default", "project_id": "shared"},
        "matches": True,
    }


def test_move_picker_marks_only_profile_matching_same_slug_project_active():
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const src = fs.readFileSync({str(SESSIONS_JS)!r}, 'utf8');

        function extractFunction(name) {{
          const marker = `function ${{name}}(`;
          const start = src.indexOf(marker);
          if (start < 0) throw new Error(name + ' not found');
          const brace = src.indexOf('{{', start);
          let depth = 0;
          for (let i = brace; i < src.length; i++) {{
            if (src[i] === '{{') depth += 1;
            else if (src[i] === '}}') {{
              depth -= 1;
              if (depth === 0) return src.slice(start, i + 1);
            }}
          }}
          throw new Error('unterminated function ' + name);
        }}

        function makeNode(tag) {{
          return {{
            tagName: String(tag || '').toUpperCase(),
            className: '',
            textContent: '',
            children: [],
            style: {{}},
            scrollWidth: 160,
            appendChild(child) {{
              this.children.push(child);
              child.parentNode = this;
              return child;
            }},
            remove() {{
              this.removed = true;
            }},
            contains(target) {{
              return this === target || this.children.includes(target);
            }},
          }};
        }}

        const bodyChildren = [];
        globalThis.window = {{ innerHeight: 800 }};
        globalThis.NO_PROJECT_FILTER = '__none__';
        globalThis.S = {{ activeProfile: 'default', activeProfileIsDefault: false, rootProfileNames: ['default'] }};
        globalThis.document = {{
          querySelectorAll() {{ return []; }},
          createElement(tag) {{ return makeNode(tag); }},
          body: {{
            appendChild(node) {{
              bodyChildren.push(node);
              return node;
            }},
          }},
          addEventListener() {{}},
          removeEventListener() {{}},
        }};
        globalThis._allProjects = [
          {{ profile: 'work', project_id: 'shared', name: 'Work', color: '#123456' }},
          {{ profile: 'default', project_id: 'shared', name: 'Default alias', color: '#abcdef' }},
        ];
        globalThis._allSessions = [{{ session_id: 's1', project_id: 'shared', profile: 'work' }}];
        globalThis.api = async () => ({{}});
        globalThis.renderSessionListFromCache = () => {{}};
        globalThis.showToast = () => {{}};
        globalThis.showPromptDialog = async () => null;
        globalThis.t = (key) => key;
        globalThis.PROJECT_COLORS = ['#111111'];
        globalThis.renderSessionList = async () => {{}};
        globalThis.setTimeout = () => 0;

        for (const name of ['_canonicalProjectFilterProfile', '_projectFilterIdentity', '_projectFilterMatches', '_showProjectPicker']) {{
          eval(extractFunction(name));
        }}

        const anchorEl = {{
          getBoundingClientRect() {{
            return {{ top: 100, bottom: 120, right: 220 }};
          }},
        }};

        _showProjectPicker({{ session_id: 's1', project_id: 'shared', profile: 'work' }}, anchorEl);
        const picker = bodyChildren[0];
        const projectItems = picker.children
          .filter((item) => item.className.startsWith('project-picker-item') && item.textContent !== 'No project' && item.textContent !== '+ New project')
          .map((item) => {{
            const nameNode = item.children[item.children.length - 1];
            return {{ name: nameNode.textContent, className: item.className }};
          }});
        console.log(JSON.stringify(projectItems));
        """
    )
    assert _run_node(script) == [
        {"name": "Work", "className": "project-picker-item active"},
        {"name": "Default alias", "className": "project-picker-item"},
    ]
