// i18n shared helpers + tool-action tables (AST-split from i18n.js).
// MUST load before the locale bundles — bundle values reference these as globals.
const _I18N_TOOL_ACTION_TEXT_EN = {
    shell: { running: 'Running', done: 'Ran', fail: 'run', fallback: 'a command' },
    read: { running: 'Reading', done: 'Read', fail: 'read', fallback: 'a file' },
    list: { running: 'Listing', done: 'Listed', fail: 'list', fallback: 'files' },
    search: { running: 'Searching', done: 'Searched', fail: 'search', fallback: 'workspace' },
    web: { running: 'Checking', done: 'Checked', fail: 'check', fallback: 'web data' },
    write: { running: 'Updating', done: 'Updated', fail: 'update', fallback: 'a file' },
    skill: { running: 'Loading', done: 'Loaded', fail: 'load', fallback: 'a skill' },
    memory: { running: 'Saving', done: 'Saved', fail: 'save', fallback: 'memory' },
    delegate: { running: 'Delegating', done: 'Delegated', fail: 'delegate', fallback: 'a task' },
    unknown: { running: 'Running', done: 'Ran', fail: 'run', fallback: 'a tool' },
};
const _I18N_TOOL_SUMMARY_TEXT_EN = {
    shell: { running: ['Running a command', 'Running {n} commands'], done: ['Ran a command', 'Ran {n} commands'] },
    read: { running: ['Reading a file', 'Reading {n} files'], done: ['Read a file', 'Read {n} files'] },
    list: { running: ['Listing files', 'Listing {n} items'], done: ['Listed files', 'Listed {n} files'] },
    search: { running: ['Searching workspace', 'Searching workspace {n} times'], done: ['Searched workspace', 'Searched workspace {n} times'] },
    web: { running: ['Checking web', 'Checking web {n} times'], done: ['Checked the web', 'Checked the web {n} times'] },
    write: { running: ['Updating a file', 'Updating {n} files'], done: ['Updated a file', 'Updated {n} files'] },
    skill: { running: ['Loading a skill', 'Loading {n} skills'], done: ['Loaded a skill', 'Loaded {n} skills'] },
    memory: { running: ['Saving memory', 'Saving {n} memory updates'], done: ['Saved memory', 'Saved {n} memory updates'] },
    delegate: { running: ['Delegating a task', 'Delegating {n} tasks'], done: ['Delegated a task', 'Delegated {n} tasks'] },
    unknown: { running: ['Running a tool', 'Running {n} tools'], done: ['Ran a tool', 'Ran {n} tools'] },
};
const _I18N_TOOL_ACTION_TEXT_ZH = {
    shell: { running: '正在运行', done: '已运行', fail: '运行', fallback: '命令' },
    read: { running: '正在读取', done: '已读取', fail: '读取', fallback: '文件' },
    list: { running: '正在列出', done: '已列出', fail: '列出', fallback: '文件' },
    search: { running: '正在搜索', done: '已搜索', fail: '搜索', fallback: '代码' },
    web: { running: '正在检查', done: '已检查', fail: '检查', fallback: '网页' },
    write: { running: '正在更新', done: '已更新', fail: '更新', fallback: '文件' },
    skill: { running: '正在读取', done: '已读取', fail: '读取', fallback: '技能' },
    memory: { running: '正在保存', done: '已保存', fail: '保存', fallback: '记忆' },
    delegate: { running: '正在委派', done: '已委派', fail: '委派', fallback: '任务' },
    unknown: { running: '正在运行', done: '已运行', fail: '运行', fallback: '工具' },
};
const _I18N_TOOL_SUMMARY_TEXT_ZH = {
    shell: { running: ['正在运行命令', '正在运行 {n} 条命令'], done: ['已运行命令', '已运行 {n} 条命令'] },
    read: { running: ['正在读取文件', '正在读取 {n} 个文件'], done: ['已读取文件', '已读取 {n} 个文件'] },
    list: { running: ['正在列出文件', '正在列出 {n} 个项目'], done: ['已列出文件', '已列出 {n} 个项目'] },
    search: { running: ['正在搜索代码', '正在搜索代码 {n} 次'], done: ['已搜索代码', '已搜索代码 {n} 次'] },
    web: { running: ['正在检查网页', '正在检查网页 {n} 次'], done: ['已检查网页', '已检查网页 {n} 次'] },
    write: { running: ['正在更新文件', '正在更新 {n} 个文件'], done: ['已更新文件', '已更新 {n} 个文件'] },
    skill: { running: ['正在读取技能', '正在读取 {n} 个技能'], done: ['已读取技能', '已读取 {n} 个技能'] },
    memory: { running: ['正在保存记忆', '正在保存 {n} 条记忆'], done: ['已保存记忆', '已保存 {n} 条记忆'] },
    delegate: { running: ['正在委派任务', '正在委派 {n} 个任务'], done: ['已委派任务', '已委派 {n} 个任务'] },
    unknown: { running: ['正在运行工具', '正在运行 {n} 个工具'], done: ['已运行工具', '已运行 {n} 个工具'] },
};
const _I18N_TOOL_ACTION_TEXT_ZH_HANT = {
    shell: { running: '正在執行', done: '已執行', fail: '執行', fallback: '命令' },
    read: { running: '正在讀取', done: '已讀取', fail: '讀取', fallback: '檔案' },
    list: { running: '正在列出', done: '已列出', fail: '列出', fallback: '檔案' },
    search: { running: '正在搜尋', done: '已搜尋', fail: '搜尋', fallback: '程式碼' },
    web: { running: '正在檢查', done: '已檢查', fail: '檢查', fallback: '網頁' },
    write: { running: '正在更新', done: '已更新', fail: '更新', fallback: '檔案' },
    skill: { running: '正在讀取', done: '已讀取', fail: '讀取', fallback: '技能' },
    memory: { running: '正在儲存', done: '已儲存', fail: '儲存', fallback: '記憶' },
    delegate: { running: '正在委派', done: '已委派', fail: '委派', fallback: '任務' },
    unknown: { running: '正在執行', done: '已執行', fail: '執行', fallback: '工具' },
};
const _I18N_TOOL_SUMMARY_TEXT_ZH_HANT = {
    shell: { running: ['正在執行命令', '正在執行 {n} 條命令'], done: ['已執行命令', '已執行 {n} 條命令'] },
    read: { running: ['正在讀取檔案', '正在讀取 {n} 個檔案'], done: ['已讀取檔案', '已讀取 {n} 個檔案'] },
    list: { running: ['正在列出檔案', '正在列出 {n} 個項目'], done: ['已列出檔案', '已列出 {n} 個項目'] },
    search: { running: ['正在搜尋程式碼', '正在搜尋程式碼 {n} 次'], done: ['已搜尋程式碼', '已搜尋程式碼 {n} 次'] },
    web: { running: ['正在檢查網頁', '正在檢查網頁 {n} 次'], done: ['已檢查網頁', '已檢查網頁 {n} 次'] },
    write: { running: ['正在更新檔案', '正在更新 {n} 個檔案'], done: ['已更新檔案', '已更新 {n} 個檔案'] },
    skill: { running: ['正在讀取技能', '正在讀取 {n} 個技能'], done: ['已讀取技能', '已讀取 {n} 個技能'] },
    memory: { running: ['正在儲存記憶', '正在儲存 {n} 條記憶'], done: ['已儲存記憶', '已儲存 {n} 條記憶'] },
    delegate: { running: ['正在委派任務', '正在委派 {n} 個任務'], done: ['已委派任務', '已委派 {n} 個任務'] },
    unknown: { running: ['正在執行工具', '正在執行 {n} 個工具'], done: ['已執行工具', '已執行 {n} 個工具'] },
};
function _i18nProcessedElapsed(prefix, duration) {
  return duration ? `${prefix} ${duration}` : prefix;
}
function _i18nProcessedElapsedEn(duration) {
  return _i18nProcessedElapsed('Processed', duration);
}
function _i18nProcessedElapsedZh(duration) {
  return _i18nProcessedElapsed('已处理', duration);
}
function _i18nProcessedElapsedZhHant(duration) {
  return _i18nProcessedElapsed('已處理', duration);
}
function _i18nToolActionLabelFromMap(map, kind, state, target, display, failed) {
  const verbs = map[kind] || map.unknown || _I18N_TOOL_ACTION_TEXT_EN.unknown;
  const object = target || verbs.fallback || display || 'tool';
  if (failed) return `Failed to ${verbs.fail || 'run'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolActionLabelEn(kind, state, target, display, failed) {
  return _i18nToolActionLabelFromMap(_I18N_TOOL_ACTION_TEXT_EN, kind, state, target, display, failed);
}
function _i18nToolActionLabelZh(kind, state, target, display, failed) {
  if (failed) {
    const verbs = _I18N_TOOL_ACTION_TEXT_ZH[kind] || _I18N_TOOL_ACTION_TEXT_ZH.unknown;
    return `未能${verbs.fail || '运行'} ${target || verbs.fallback || display || '工具'}`;
  }
  return _i18nToolActionLabelFromMap(_I18N_TOOL_ACTION_TEXT_ZH, kind, state, target, display, failed);
}
function _i18nToolActionLabelZhHant(kind, state, target, display, failed) {
  if (failed) {
    const verbs = _I18N_TOOL_ACTION_TEXT_ZH_HANT[kind] || _I18N_TOOL_ACTION_TEXT_ZH_HANT.unknown;
    return `未能${verbs.fail || '執行'} ${target || verbs.fallback || display || '工具'}`;
  }
  return _i18nToolActionLabelFromMap(_I18N_TOOL_ACTION_TEXT_ZH_HANT, kind, state, target, display, failed);
}
function _i18nToolWorklogSummaryFromMap(map, kind, state, count) {
  const n = Math.max(1, Number(count) || 1);
  const form = (map[kind] || map.unknown || _I18N_TOOL_SUMMARY_TEXT_EN.unknown)[state] || map.unknown.running;
  return (n === 1 ? form[0] : form[1]).replace('{n}', String(n));
}
function _i18nToolWorklogSummaryEn(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_EN, kind, state, count);
}
function _i18nToolWorklogSummaryZh(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_ZH, kind, state, count);
}
function _i18nToolWorklogSummaryZhHant(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_ZH_HANT, kind, state, count);
}
function _i18nToolSummaryJoinEn(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}
function _i18nToolSummaryJoinCjk(parts) {
  const items = Array.isArray(parts) ? parts.filter(Boolean) : [];
  if (items.length <= 1) return items[0] || '';
  if (items.length === 2) return `${items[0]}和${items[1]}`;
  return `${items.slice(0, -1).join('、')}和${items[items.length - 1]}`;
}

const _I18N_TOOL_ACTION_TEXT_VI = {
    shell: { running: 'Đang chạy', done: 'Đã chạy', fail: 'chạy', fallback: 'lệnh' },
    read: { running: 'Đang đọc', done: 'Đã đọc', fail: 'đọc', fallback: 'tệp' },
    list: { running: 'Đang liệt kê', done: 'Đã liệt kê', fail: 'liệt kê', fallback: 'tệp' },
    search: { running: 'Đang tìm kiếm', done: 'Đã tìm kiếm', fail: 'tìm kiếm', fallback: 'workspace' },
    web: { running: 'Đang kiểm tra', done: 'Đã kiểm tra', fail: 'kiểm tra', fallback: 'dữ liệu web' },
    write: { running: 'Đang cập nhật', done: 'Đã cập nhật', fail: 'cập nhật', fallback: 'tệp' },
    skill: { running: 'Đang tải', done: 'Đã tải', fail: 'tải', fallback: 'kỹ năng' },
    memory: { running: 'Đang lưu', done: 'Đã lưu', fail: 'lưu', fallback: 'bộ nhớ' },
    delegate: { running: 'Đang ủy quyền', done: 'Đã ủy quyền', fail: 'ủy quyền', fallback: 'tác vụ' },
    unknown: { running: 'Đang chạy', done: 'Đã chạy', fail: 'chạy', fallback: 'công cụ' },
};
const _I18N_TOOL_SUMMARY_TEXT_VI = {
    shell: { running: ['Đang chạy lệnh', 'Đang chạy {n} lệnh'], done: ['Đã chạy lệnh', 'Đã chạy {n} lệnh'] },
    read: { running: ['Đang đọc tệp', 'Đang đọc {n} tệp'], done: ['Đã đọc tệp', 'Đã đọc {n} tệp'] },
    list: { running: ['Đang liệt kê tệp', 'Đang liệt kê {n} mục'], done: ['Đã liệt kê tệp', 'Đã liệt kê {n} tệp'] },
    search: { running: ['Đang tìm kiếm workspace', 'Đang tìm kiếm workspace {n} lần'], done: ['Đã tìm kiếm workspace', 'Đã tìm kiếm workspace {n} lần'] },
    web: { running: ['Đang kiểm tra web', 'Đang kiểm tra web {n} lần'], done: ['Đã kiểm tra web', 'Đã kiểm tra web {n} lần'] },
    write: { running: ['Đang cập nhật tệp', 'Đang cập nhật {n} tệp'], done: ['Đã cập nhật tệp', 'Đã cập nhật {n} tệp'] },
    skill: { running: ['Đang tải kỹ năng', 'Đang tải {n} kỹ năng'], done: ['Đã tải kỹ năng', 'Đã tải {n} kỹ năng'] },
    memory: { running: ['Đang lưu bộ nhớ', 'Đang lưu {n} cập nhật bộ nhớ'], done: ['Đã lưu bộ nhớ', 'Đã lưu {n} cập nhật bộ nhớ'] },
    delegate: { running: ['Đang ủy quyền tác vụ', 'Đang ủy quyền {n} tác vụ'], done: ['Đã ủy quyền tác vụ', 'Đã ủy quyền {n} tác vụ'] },
    unknown: { running: ['Đang chạy công cụ', 'Đang chạy {n} công cụ'], done: ['Đã chạy công cụ', 'Đã chạy {n} công cụ'] },
};
function _i18nProcessedElapsedVi(duration) {
  return _i18nProcessedElapsed('Đã xử lý', duration);
}
function _i18nToolActionLabelVi(kind, state, target, display, failed) {
  const verbs = _I18N_TOOL_ACTION_TEXT_VI[kind] || _I18N_TOOL_ACTION_TEXT_VI.unknown;
  const object = target || verbs.fallback || display || 'công cụ';
  if (failed) return `Không thể ${verbs.fail || 'chạy'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolWorklogSummaryVi(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_VI, kind, state, count);
}

const _I18N_TOOL_ACTION_TEXT_PL = {
    shell: { running: 'Uruchamianie', done: 'Uruchomiono', fail: 'uruchomić', fallback: 'polecenie' },
    read: { running: 'Odczytywanie', done: 'Odczytano', fail: 'odczytać', fallback: 'plik' },
    list: { running: 'Listowanie', done: 'Wylistowano', fail: 'wylistować', fallback: 'plik' },
    search: { running: 'Przeszukiwanie', done: 'Przeszukano', fail: 'przeszukać', fallback: 'obszar roboczy' },
    web: { running: 'Sprawdzanie', done: 'Sprawdzono', fail: 'sprawdzić', fallback: 'dane internetowe' },
    write: { running: 'Zapisywanie', done: 'Zapisano', fail: 'zapisać', fallback: 'plik' },
    skill: { running: 'Wczytywanie', done: 'Wczytano', fail: 'wczytać', fallback: 'umiejętność' },
    memory: { running: 'Zapisywanie', done: 'Zapisano', fail: 'zapisać', fallback: 'pamięć' },
    delegate: { running: 'Delegowanie', done: 'Delegowano', fail: 'delegować', fallback: 'zadanie' },
    unknown: { running: 'Uruchamianie', done: 'Uruchomiono', fail: 'uruchomić', fallback: 'narzędzie' },
};
const _I18N_TOOL_SUMMARY_TEXT_PL = {
    shell: { running: ['Uruchamianie polecenia', 'Uruchamianie {n} poleceń'], done: ['Uruchomiono polecenie', 'Uruchomiono {n} poleceń'] },
    read: { running: ['Odczytywanie pliku', 'Odczytywanie {n} plików'], done: ['Odczytano plik', 'Odczytano {n} plików'] },
    list: { running: ['Listowanie pliku', 'Listowanie {n} plików'], done: ['Wylistowano plik', 'Wylistowano {n} plików'] },
    search: { running: ['Przeszukiwanie obszaru roboczego', 'Przeszukiwanie {n}-krotne obszaru roboczego'], done: ['Przeszukano obszar roboczy', 'Przeszukano {n}-krotnie obszar roboczy'] },
    web: { running: ['Sprawdzanie stron', 'Sprawdzanie {n} stron'], done: ['Sprawdzono strony', 'Sprawdzono {n} stron'] },
    write: { running: ['Zapisywanie pliku', 'Zapisywanie {n} plików'], done: ['Zapisano plik', 'Zapisano {n} plików'] },
    skill: { running: ['Wczytywanie umiejętności', 'Wczytywanie {n} umiejętności'], done: ['Wczytano umiejętność', 'Wczytano {n} umiejętności'] },
    memory: { running: ['Zapisywanie pamięci', 'Zapisywanie {n} wpisów pamięci'], done: ['Zapisano pamięć', 'Zapisano {n} wpisów pamięci'] },
    delegate: { running: ['Delegowanie zadania', 'Delegowanie {n} zadań'], done: ['Delegowano zadanie', 'Delegowano {n} zadań'] },
    unknown: { running: ['Uruchamianie narzędzia', 'Uruchamianie {n} narzędzi'], done: ['Uruchomiono narzędzie', 'Uruchomiono {n} narzędzi'] },
};
function _i18nProcessedElapsedPl(duration) {
  return _i18nProcessedElapsed('Przetworzono', duration);
}
function _i18nToolActionLabelPl(kind, state, target, display, failed) {
  const verbs = _I18N_TOOL_ACTION_TEXT_PL[kind] || _I18N_TOOL_ACTION_TEXT_PL.unknown;
  const object = target || verbs.fallback || display || 'narzędzie';
  if (failed) return `Nie udało się ${verbs.fail || 'uruchomić'} ${object}`;
  return `${verbs[state] || verbs.running} ${object}`;
}
function _i18nToolWorklogSummaryPl(kind, state, count) {
  return _i18nToolWorklogSummaryFromMap(_I18N_TOOL_SUMMARY_TEXT_PL, kind, state, count);
}
function _i18nToolSummaryJoinPl(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}
function _i18nToolSummaryJoinVi(parts) {
  return Array.isArray(parts) ? parts.filter(Boolean).join(', ') : '';
}



// Active locale — defaults to English; overridden by loadLocale() at boot.
