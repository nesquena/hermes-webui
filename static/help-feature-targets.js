// Audited inert navigation metadata for the complete WebUI feature catalogue.
// A selector is a candidate, never evidence of current visibility or permission.
// Empty targets mean explanatory fallback: no exact parent-DOM hook is available.
// No clicks, callbacks, network URLs, mutations or iframe traversal belong here.
(function () {
  'use strict';
  const entries = {
  "webui.chat.bot_screens": {
    "panel": "chat",
    "targets": [
      "#workspaceScreensTab"
    ],
    "entryTargets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle"
    ],
    "entryHint": {
      "nl": "Open het rechterzijpaneel en kies Bot Screens. Het tabblad verschijnt als je schermrechten hebt.",
      "en": "Open the right panel and choose Bot Screens. The tab appears when you have screen access."
    }
  },
  "webui.bots.org_chart": {
    "panel": "profiles",
    "targets": [
      ".bot-directory-chart",
      "#btnBotOrgChart"
    ],
    "entryTargets": [
      ".rail [data-panel=\"profiles\"]"
    ],
    "entryHint": {
      "nl": "Open Bots en kies bovenaan de lijst Organigram. Rapporteert aan stel je per bot in bij Bewerken.",
      "en": "Open Bots and choose Org chart at the top of the list. Set Reports to per bot under Edit."
    }
  },
  "webui.bots.screens": {
    "panel": "profiles",
    "targets": [
      "#btnBotScreens"
    ],
    "entryTargets": [
      ".rail [data-panel=\"profiles\"]"
    ],
    "entryHint": {
      "nl": "Open Bots. Met expliciete schermrechten verschijnt daar Bot Screens; zonder die rechten kan je beheerder toegang instellen.",
      "en": "Open Bots. Bot Screens appears with explicit screen grants; otherwise your administrator can configure access."
    }
  },
  "webui.help.navigation": {
    "panel": "chat",
    "targets": [
      "#btnHamburger",
      "#railExpandToggle"
    ]
  },
  "webui.help.tour": {
    "panel": "settings",
    "section": "help",
    "targets": [
      "#helpCardStartTour"
    ]
  },
  "webui.help.org_tour": {
    "panel": "settings",
    "section": "help",
    "targets": [
      "#helpCardStartOrgTour"
    ]
  },
  "webui.help.page": {
    "panel": "chat",
    "targets": [
      "#btnPageHelp"
    ]
  },
  "webui.help.guide": {
    "panel": "settings",
    "section": "help",
    "targets": [
      "#helpCardGuide"
    ]
  },
  "webui.help.confirm": {
    "panel": "chat",
    "targets": [
      "#appDialogClose",
      "#appDialogInput",
      "#appDialogCancel",
      "#appDialogConfirm"
    ]
  },
  "webui.chat.send": {
    "panel": "chat",
    "targets": [
      "#msg",
      "#btnSend"
    ]
  },
  "webui.chat.normal": {
    "panel": "chat",
    "targets": [
      "#chatModeNormal"
    ]
  },
  "webui.chat.super": {
    "panel": "chat",
    "targets": [
      "#chatModeSuper"
    ]
  },
  "webui.chat.model": {
    "panel": "chat",
    "targets": [
      "#composerModelChip",
      "#modelSelect",
      "#composerMobileModelAction"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.chat.reasoning": {
    "panel": "chat",
    "targets": [
      "#composerReasoningChip",
      "#composerMobileReasoningAction"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.chat.quota": {
    "panel": "chat",
    "targets": [
      "#providerQuotaChip",
      "#composerMobileQuotaAction"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.chat.context": {
    "panel": "chat",
    "targets": [
      "#ctxIndicator",
      "#ctxCompressBtn",
      "#composerMobileCtxCompressBtn"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.chat.tools": {
    "panel": "chat",
    "targets": [
      "#composerToolsetsChip",
      "#toolsetsInput",
      "#toolsetsApplyBtn",
      "#toolsetsClearBtn"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.chat.attach": {
    "panel": "chat",
    "targets": [
      "#fileInput",
      "#btnAttach"
    ]
  },
  "webui.chat.prompts": {
    "panel": "chat",
    "targets": [
      "#btnSavedPrompts"
    ]
  },
  "webui.chat.people": {
    "panel": "chat",
    "targets": [
      "#composerPeopleChip",
      "#groupPeopleFilter",
      "#groupPeopleModalSubmit"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.chat.mentions": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.scroll": {
    "panel": "chat",
    "targets": [
      "#jumpToSessionStartBtn",
      "#scrollToBottomBtn",
      "#outlineToggleBtn"
    ]
  },
  "webui.chat.copy": {
    "panel": "chat",
    "targets": [
      ".msg-copy-btn"
    ]
  },
  "webui.chat.edit": {
    "panel": "chat",
    "targets": [
      "[data-help-action='edit-message']",
      ".msg-edit-area"
    ]
  },
  "webui.chat.undo": {
    "panel": "chat",
    "targets": []
  },
  "webui.chat.retry": {
    "panel": "chat",
    "targets": [
      "[data-help-action='retry-response']"
    ]
  },
  "webui.chat.fork": {
    "panel": "chat",
    "targets": [
      "[data-help-action='fork-message']"
    ]
  },
  "webui.chat.activity": {
    "panel": "chat",
    "targets": [
      ".tool-call-group-summary",
      ".tool-worklog-tool-group-head"
    ]
  },
  "webui.chat.svg": {
    "panel": "chat",
    "targets": [
      ".svg-visual-block",
      ".msg-media-svg"
    ]
  },
  "webui.chat.replay": {
    "panel": "chat",
    "targets": []
  },
  "webui.chat.mobile": {
    "panel": "chat",
    "targets": [
      "#composerMobileConfigBtn"
    ]
  },
  "webui.sessions.new": {
    "panel": "chat",
    "targets": [
      "#btnTitlebarNewChat",
      "#btnNewChat"
    ]
  },
  "webui.sessions.search": {
    "panel": "chat",
    "targets": [
      "#sessionSearch",
      "#sessionSearchClear"
    ],
    "entryTargets": [
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar de chatzijbalk aan. Open deze na de uitleg om de gesprekken en hun bedieningen te zien.",
      "en": "The highlight marks the chat sidebar entry. Open it after the guide to see conversations and their controls."
    }
  },
  "webui.sessions.split": {
    "panel": "chat",
    "targets": [
      "#btnSplitView",
      "#btnSplitExit"
    ],
    "entryTargets": [
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar de chatzijbalk aan. Open deze na de uitleg om de gesprekken en hun bedieningen te zien.",
      "en": "The highlight marks the chat sidebar entry. Open it after the guide to see conversations and their controls."
    }
  },
  "webui.sessions.transcript": {
    "panel": "settings",
    "section": "conversation",
    "targets": [
      "#btnDownload"
    ]
  },
  "webui.sessions.json": {
    "panel": "settings",
    "section": "conversation",
    "targets": [
      "#btnExportJSON",
      "#btnImportJSON",
      "#importFileInput"
    ]
  },
  "webui.sessions.html": {
    "panel": "settings",
    "section": "conversation",
    "targets": [
      "#btnExportHTML"
    ]
  },
  "webui.sessions.share": {
    "panel": "settings",
    "section": "conversation",
    "targets": [
      "#btnShareSession",
      "#btnStopSharingSession"
    ]
  },
  "webui.sessions.clear": {
    "panel": "settings",
    "section": "conversation",
    "targets": [
      "#btnClearConvModal"
    ]
  },
  "webui.sessions.organize": {
    "panel": "chat",
    "targets": [
      ".session-actions-trigger"
    ],
    "entryTargets": [
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar de chatzijbalk aan. Open deze na de uitleg om de gesprekken en hun bedieningen te zien.",
      "en": "The highlight marks the chat sidebar entry. Open it after the guide to see conversations and their controls."
    }
  },
  "webui.voice.dictation": {
    "panel": "chat",
    "targets": [
      "#btnMic"
    ]
  },
  "webui.voice.read": {
    "panel": "chat",
    "targets": [
      ".msg-tts-btn"
    ]
  },
  "webui.voice.loop": {
    "panel": "chat",
    "targets": [
      "#btnVoiceMode"
    ]
  },
  "webui.voice.realtime": {
    "panel": "chat",
    "targets": [
      "#realtimeVoiceEnd",
      "#btnRealtimeVoice"
    ]
  },
  "webui.voice.hold": {
    "panel": "chat",
    "targets": [
      "#realtimeVoiceHold",
      "#realtimeVoiceMic",
      "#realtimeVoiceInterrupt"
    ],
    "entryTargets": [
      "#btnRealtimeVoice"
    ],
    "entryHint": {
      "nl": "De markering wijst Realtime spraak aan. De beschreven microfoonbediening verschijnt tijdens een actieve realtime spraaksessie. Start die alleen wanneer je zelf wilt spreken.",
      "en": "The highlight marks Realtime voice. The described microphone control appears during an active realtime voice session. Start one only when you want to speak."
    }
  },
  "webui.tasks.create": {
    "panel": "tasks",
    "targets": [
      "[data-tour='tasks-new']",
      "#cronFormName"
    ]
  },
  "webui.tasks.prompt": {
    "panel": "tasks",
    "targets": [
      "#cronFormPrompt"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg dit formulier om de beschreven velden te zien. Er wordt pas een taak aangemaakt wanneer je die zelf opslaat.",
      "en": "The highlight marks New task. Open this form after the guide to see the described fields. A task is created only when you save it yourself."
    }
  },
  "webui.tasks.script": {
    "panel": "tasks",
    "targets": [
      "#cronFormScript",
      ".cron-script-card"
    ],
    "entryTargets": [
      ".cron-item:has(.cron-script-badge)"
    ],
    "entryHint": {
      "nl": "De markering wijst een bestaande scripttaak aan. Open na de uitleg de gewenste scripttaak om het scriptpad te bekijken. Nieuwe taak maakt een agenttaak; het scriptbestand wijzig je op de server.",
      "en": "The highlight marks an existing script task. Open the intended script task after the guide to inspect its script path. New task creates an agent task; edit the script file on the server."
    }
  },
  "webui.tasks.skills": {
    "panel": "tasks",
    "targets": [
      "#cronFormSkillSearch"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg dit formulier om de beschreven velden te zien. Er wordt pas een taak aangemaakt wanneer je die zelf opslaat.",
      "en": "The highlight marks New task. Open this form after the guide to see the described fields. A task is created only when you save it yourself."
    }
  },
  "webui.tasks.schedule": {
    "panel": "tasks",
    "targets": [
      "#cronFormSchedulePreset",
      "#cronFormScheduleWeekday",
      "#cronFormScheduleMonthDay",
      "#cronFormScheduleTime",
      "#cronFormScheduleMinute",
      "#cronFormSchedule"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg dit formulier om de beschreven velden te zien. Er wordt pas een taak aangemaakt wanneer je die zelf opslaat.",
      "en": "The highlight marks New task. Open this form after the guide to see the described fields. A task is created only when you save it yourself."
    }
  },
  "webui.tasks.profile": {
    "panel": "tasks",
    "targets": [
      "#cronFormProfile",
      "#cronFormModel"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg dit formulier om de beschreven velden te zien. Er wordt pas een taak aangemaakt wanneer je die zelf opslaat.",
      "en": "The highlight marks New task. Open this form after the guide to see the described fields. A task is created only when you save it yourself."
    }
  },
  "webui.tasks.delivery": {
    "panel": "tasks",
    "targets": [
      "#cronFormDeliver"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg dit formulier om de beschreven velden te zien. Er wordt pas een taak aangemaakt wanneer je die zelf opslaat.",
      "en": "The highlight marks New task. Open this form after the guide to see the described fields. A task is created only when you save it yourself."
    }
  },
  "webui.tasks.notify": {
    "panel": "tasks",
    "targets": [
      "[data-tour='tasks-notify']"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg het formulier en ga naar Meldingen. Je kunt de meldingen daar instellen voordat je de taak opslaat.",
      "en": "The highlight marks New task. After the guide, open the form and find Notifications. You can configure notifications there before saving the task."
    }
  },
  "webui.tasks.mute": {
    "panel": "tasks",
    "targets": [
      ".cron-notify-mute-btn",
      ".cron-notify-mine"
    ],
    "entryTargets": [
      ".cron-item"
    ],
    "entryHint": {
      "nl": "De markering wijst een taak in de lijst aan. Open na de uitleg de gewenste taak om de meldingen voor die taak te bekijken.",
      "en": "The highlight marks a task in the list. Open the intended task after the guide to see its notification controls."
    }
  },
  "webui.tasks.run": {
    "panel": "tasks",
    "targets": [
      "#btnRunTaskDetail"
    ],
    "entryTargets": [
      ".cron-item:not(.readonly)"
    ],
    "entryHint": {
      "nl": "De markering wijst een geplande taak in de lijst aan. Open na de uitleg de taak die je wilt beheren; de beschreven acties staan in de taakdetails.",
      "en": "The highlight marks a scheduled task in the list. After the guide, open the task you want to manage; its detail view contains the described actions."
    }
  },
  "webui.tasks.pause": {
    "panel": "tasks",
    "targets": [
      "#btnPauseTaskDetail",
      "#btnResumeTaskDetail"
    ],
    "entryTargets": [
      ".cron-item:not(.readonly)"
    ],
    "entryHint": {
      "nl": "De markering wijst een geplande taak in de lijst aan. Open na de uitleg de taak die je wilt beheren; de beschreven acties staan in de taakdetails.",
      "en": "The highlight marks a scheduled task in the list. After the guide, open the task you want to manage; its detail view contains the described actions."
    }
  },
  "webui.tasks.share": {
    "panel": "tasks",
    "targets": [
      "#btnShareTaskDetail",
      "#cronShareExtra",
      "#cronShareCancel",
      "#cronShareSave"
    ],
    "entryTargets": [
      ".cron-item:not(.readonly)"
    ],
    "entryHint": {
      "nl": "De markering wijst een geplande taak in de lijst aan. Open na de uitleg de taak die je wilt beheren; de beschreven acties staan in de taakdetails.",
      "en": "The highlight marks a scheduled task in the list. After the guide, open the task you want to manage; its detail view contains the described actions."
    }
  },
  "webui.tasks.edit": {
    "panel": "tasks",
    "targets": [
      "#btnEditTaskDetail",
      "#btnDuplicateTaskDetail",
      "#btnDeleteTaskDetail"
    ],
    "entryTargets": [
      ".cron-item:not(.readonly)"
    ],
    "entryHint": {
      "nl": "De markering wijst een geplande taak in de lijst aan. Open na de uitleg de taak die je wilt beheren; de beschreven acties staan in de taakdetails.",
      "en": "The highlight marks a scheduled task in the list. After the guide, open the task you want to manage; its detail view contains the described actions."
    }
  },
  "webui.tasks.labels": {
    "panel": "tasks",
    "targets": [
      "#cronEmojiCustom",
      "#cronCategoryPick",
      "#cronFormEmoji",
      "#cronFormCategory",
      "#cronFormDiagram"
    ],
    "entryTargets": [
      "[data-tour='tasks-new']"
    ],
    "entryHint": {
      "nl": "De markering wijst Nieuwe taak aan. Open na de uitleg dit formulier om de beschreven velden te zien. Er wordt pas een taak aangemaakt wanneer je die zelf opslaat.",
      "en": "The highlight marks New task. Open this form after the guide to see the described fields. A task is created only when you save it yourself."
    }
  },
  "webui.tasks.results": {
    "panel": "tasks",
    "targets": [
      "#cronDetailRuns"
    ],
    "entryTargets": [
      ".cron-item:not(.readonly)"
    ],
    "entryHint": {
      "nl": "De markering wijst een geplande taak aan. Open na de uitleg de gewenste taak en bekijk de uitvoer onderaan de details. Er zijn pas resultaten nadat die taak is uitgevoerd.",
      "en": "The highlight marks a scheduled task. After the guide, open the intended task and review output at the bottom of its details. Results exist only after that task has run."
    }
  },
  "webui.files.workspace": {
    "panel": "chat",
    "targets": [
      "#composerWorkspaceChip",
      "#composerMobileWorkspaceAction"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de chatinstellingen aan. Open dit menu na de uitleg om deze bediening op een smal scherm te vinden.",
      "en": "The highlight marks chat settings. Open this menu after the guide to find this control on a narrow screen."
    }
  },
  "webui.files.manage": {
    "panel": "workspaces",
    "targets": [
      "#btnEditWorkspaceDetail",
      "#btnDeleteWorkspaceDetail",
      "#btnCancelWorkspaceDetail",
      "#btnSaveWorkspaceDetail"
    ]
  },
  "webui.files.access": {
    "panel": "workspaces",
    "targets": [
      "#wsAssignOwner",
      "#wsAssignMembers",
      "#wsAssignSaveBtn"
    ]
  },
  "webui.files.browse": {
    "panel": "chat",
    "targets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      "#btnUpDir",
      "#btnRefreshPanel",
      "#btnClearPreview",
      "#workspaceFilesTab"
    ]
  },
  "webui.files.create": {
    "panel": "chat",
    "targets": [
      "#btnNewFile",
      "#btnNewFolder"
    ],
    "entryTargets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de knop voor het bestandenpaneel aan. Open het paneel na de uitleg; daar vind je deze bediening voor de actieve werkruimte.",
      "en": "The highlight marks the files panel button. Open that panel after the guide to find this control for the active workspace."
    }
  },
  "webui.files.upload": {
    "panel": "chat",
    "targets": [
      "#btnUploadWorkspace",
      "#workspaceFileInput"
    ],
    "entryTargets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de knop voor het bestandenpaneel aan. Open het paneel na de uitleg; daar vind je deze bediening voor de actieve werkruimte.",
      "en": "The highlight marks the files panel button. Open that panel after the guide to find this control for the active workspace."
    }
  },
  "webui.files.preview": {
    "panel": "chat",
    "targets": [
      "#previewArea",
      "#btnRenderMarkdownAnyway"
    ],
    "entryTargets": [
      "#fileTree .file-item[data-ws-type=\"file\"]",
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar bestanden aan. Open na de uitleg het bestandenpaneel en kies een bestand om de voorvertoning en bestandsacties te zien.",
      "en": "The highlight marks the entry to files. After the guide, open the files panel and choose a file to see its preview and file actions."
    }
  },
  "webui.files.edit": {
    "panel": "chat",
    "targets": [
      "#btnEditFile",
      "#previewEditArea"
    ],
    "entryTargets": [
      "#fileTree .file-item[data-ws-type=\"file\"]",
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar bestanden aan. Open na de uitleg het bestandenpaneel en kies een bestand om de voorvertoning en bestandsacties te zien.",
      "en": "The highlight marks the entry to files. After the guide, open the files panel and choose a file to see its preview and file actions."
    }
  },
  "webui.files.download": {
    "panel": "chat",
    "targets": [
      "#btnOpenInBrowser",
      "#btnDownloadFile"
    ],
    "entryTargets": [
      "#fileTree .file-item[data-ws-type=\"file\"]",
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar bestanden aan. Open na de uitleg het bestandenpaneel en kies een bestand om de voorvertoning en bestandsacties te zien.",
      "en": "The highlight marks the entry to files. After the guide, open the files panel and choose a file to see its preview and file actions."
    }
  },
  "webui.files.path": {
    "panel": "chat",
    "targets": [
      "#btnCopyPreviewRelPath"
    ],
    "entryTargets": [
      "#fileTree .file-item[data-ws-type=\"file\"]",
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar bestanden aan. Open na de uitleg het bestandenpaneel en kies een bestand om de voorvertoning en bestandsacties te zien.",
      "en": "The highlight marks the entry to files. After the guide, open the files panel and choose a file to see its preview and file actions."
    }
  },
  "webui.files.artifacts": {
    "panel": "chat",
    "targets": [
      "#workspaceArtifactsTab"
    ],
    "entryTargets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de knop voor het bestandenpaneel aan. Open het paneel na de uitleg; daar vind je deze bediening voor de actieve werkruimte.",
      "en": "The highlight marks the files panel button. Open that panel after the guide to find this control for the active workspace."
    }
  },
  "webui.files.todos": {
    "panel": "chat",
    "targets": [
      "#workspaceTodosTab"
    ],
    "entryTargets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de knop voor het bestandenpaneel aan. Open het paneel na de uitleg; daar vind je deze bediening voor de actieve werkruimte.",
      "en": "The highlight marks the files panel button. Open that panel after the guide to find this control for the active workspace."
    }
  },
  "webui.files.preferences": {
    "panel": "chat",
    "targets": [
      "#btnWorkspacePrefs"
    ],
    "entryTargets": [
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de knop voor het bestandenpaneel aan. Open het paneel na de uitleg; daar vind je deze bediening voor de actieve werkruimte.",
      "en": "The highlight marks the files panel button. Open that panel after the guide to find this control for the active workspace."
    }
  },
  "webui.boards.create": {
    "panel": "kanban",
    "targets": [
      "#kanbanNewTaskBtn",
      "#kanbanNewTaskTitle",
      "#kanbanTaskModalTitleInput",
      "#kanbanTaskModalBody",
      "#kanbanTaskModalSubmit"
    ]
  },
  "webui.boards.status": {
    "panel": "kanban",
    "targets": [
      "#kanbanTaskModalStatus",
      "#kanbanTaskModalPriority"
    ]
  },
  "webui.boards.assignment": {
    "panel": "kanban",
    "targets": [
      "#kanbanTaskModalAssignee",
      "#kanbanTaskModalTenant"
    ]
  },
  "webui.boards.runtime": {
    "panel": "kanban",
    "targets": [
      "#kanbanTaskModalWorkspaceKind",
      "#kanbanTaskModalWorkspacePath",
      "#kanbanTaskModalSkills",
      "#kanbanTaskModalMaxRuntimeSeconds"
    ]
  },
  "webui.boards.links": {
    "panel": "kanban",
    "targets": [
      "#kanbanTaskModalParents",
      "#kanbanDependencyInput"
    ]
  },
  "webui.boards.comments": {
    "panel": "kanban",
    "targets": [
      "#kanbanCommentInput"
    ]
  },
  "webui.boards.block": {
    "panel": "kanban",
    "targets": [
      "#kanbanTaskModalStatus"
    ]
  },
  "webui.boards.filter": {
    "panel": "kanban",
    "targets": [
      "#kanbanRefreshBtn",
      "#kanbanSearch",
      "#kanbanAssigneeFilter",
      "#kanbanTenantFilter",
      "#kanbanIncludeArchived",
      "#kanbanOnlyMine"
    ]
  },
  "webui.boards.bulk": {
    "panel": "kanban",
    "targets": [
      "#kanbanBulkStatus"
    ]
  },
  "webui.boards.dispatch": {
    "panel": "kanban",
    "targets": [
      "#btnKanbanPreviewDispatcher",
      "#btnKanbanRunDispatcher"
    ]
  },
  "webui.boards.boards": {
    "panel": "kanban",
    "targets": [
      "#kanbanBoardSwitcherToggle",
      "#btnKanbanCreateBoard",
      "#kanbanBoardModalMode",
      "#kanbanBoardModalSlug",
      "#kanbanBoardModalName",
      "#kanbanBoardModalSlugInput",
      "#kanbanBoardModalDesc",
      "#kanbanBoardModalIcon",
      "#kanbanBoardModalColor",
      "#kanbanBoardModalSubmit"
    ]
  },
  "webui.boards.consolidated": {
    "panel": "kanban",
    "targets": [
      "#btnKanbanViewToggle"
    ]
  },
  "webui.memory.personal": {
    "panel": "memory",
    "targets": [
      "#btnEditMemoryDetail",
      "#btnCancelMemoryDetail",
      "#btnSaveMemoryDetail",
      "#memEditContent"
    ]
  },
  "webui.memory.company": {
    "panel": "memory",
    "targets": [
      "#memoryPanel button[title='All agents can use this knowledge · Admin management']"
    ]
  },
  "webui.memory.project": {
    "panel": "memory",
    "targets": [
      "#memoryPanel button[title='Shared with project members · Read only']"
    ]
  },
  "webui.memory.identity": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.external": {
    "panel": "memory",
    "targets": [
      "#externalNotesSource",
      "#externalNotesQuery"
    ]
  },
  "webui.memory.explorer": {
    "panel": "memory",
    "targets": [
      "#memdashPicker",
      "[data-memdash='explorer']"
    ]
  },
  "webui.memory.search": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.audit": {
    "panel": "memory",
    "targets": [
      "[data-memdash='access']"
    ]
  },
  "webui.skills.browse": {
    "panel": "skills",
    "targets": [
      "#skillsSearch"
    ]
  },
  "webui.skills.edit": {
    "panel": "skills",
    "targets": [
      "#btnEditSkillDetail",
      "#btnCancelSkillDetail",
      "#btnSaveSkillDetail",
      "#skillFormName",
      "#skillFormCategory",
      "#skillFormContent"
    ]
  },
  "webui.skills.delete": {
    "panel": "skills",
    "targets": [
      "#btnDeleteSkillDetail"
    ]
  },
  "webui.skills.wiki": {
    "panel": "skills",
    "targets": [
      "#wikiBrowserSearch"
    ],
    "entryTargets": [
      ".wiki-browse-btn"
    ],
    "entryHint": {
      "nl": "De markering wijst Bladeren in de wiki aan. Open dit venster na de uitleg om de wikipagina's te zoeken en te lezen.",
      "en": "The highlight marks Browse wiki. Open that window after the guide to search and read wiki pages."
    }
  },
  "webui.bots.select": {
    "panel": "chat",
    "targets": [
      "#profileChip",
      "#titlebarProfileBtn"
    ],
    "entryTargets": [
      "#composerMobileConfigBtn"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.create": {
    "panel": "profiles",
    "targets": [
      "[data-tour='bots-new']",
      "#builderName"
    ]
  },
  "webui.bots.identity": {
    "panel": "profiles",
    "targets": [
      "#builderName",
      "#builderTitle",
      "#builderDescription"
    ],
    "entryTargets": [
      "[data-builder-tab='0']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.instructions": {
    "panel": "profiles",
    "targets": [
      "#builderPrompt"
    ],
    "entryTargets": [
      "[data-builder-tab='1']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.skills": {
    "panel": "profiles",
    "targets": [
      "[data-selection='skills']",
      "[data-filter='skills']"
    ],
    "entryTargets": [
      "[data-builder-tab='5']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.tools": {
    "panel": "profiles",
    "targets": [
      "[data-selection='mcp_servers']",
      "[data-selection='cli_tools']"
    ],
    "entryTargets": [
      "[data-builder-tab='2']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.memory": {
    "panel": "profiles",
    "targets": [
      "#builderMemory"
    ],
    "entryTargets": [
      "[data-builder-tab='4']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.knowledge": {
    "panel": "profiles",
    "targets": [
      "#builderKnowledge [data-knowledge-save]",
      "#builderKnowledge"
    ],
    "entryTargets": [
      "[data-builder-tab='4']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.access": {
    "panel": "profiles",
    "targets": [
      "[data-selection='allowed_users']",
      "[data-selection='allowed_groups']"
    ],
    "entryTargets": [
      "[data-builder-tab='3']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.appearance": {
    "panel": "profiles",
    "targets": [
      "#builderPhoto",
      "#botTitle",
      "#botDescription",
      "#botShape",
      "#botColor",
      "#botCategory",
      "#botAvatarFile"
    ],
    "entryTargets": [
      "[data-builder-tab='0']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.manage": {
    "panel": "profiles",
    "targets": [
      "#btnDeleteProfileDetail",
      "#btnCancelProfileDetail",
      "#btnSaveProfileDetail"
    ],
    "entryTargets": [
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst een bot in de lijst aan. Open na de uitleg de bot die je wilt beheren om de beschikbare acties te zien.",
      "en": "The highlight marks a bot in the list. Open the bot you want to manage after the guide to see its available actions."
    }
  },
  "webui.bots.scout": {
    "panel": "profiles",
    "targets": [
      "#botScoutSearch",
      "#botScoutFrequency",
      "#botScoutPrompt",
      "#botScoutSave",
      "#botScoutPause"
    ],
    "entryTargets": [
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst een bot in de lijst aan. Open na de uitleg een bot om dit onderdeel op zijn pagina te zien.",
      "en": "The highlight marks a bot in the list. Open a bot after the guide to see this part on its page."
    }
  },
  "webui.bots.responsibilities": {
    "panel": "profiles",
    "targets": [
      "#botDutiesCard",
      "#botDutyAdd"
    ],
    "entryTargets": [
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst een bot in de lijst aan. Open na de uitleg een bot om dit onderdeel op zijn pagina te zien.",
      "en": "The highlight marks a bot in the list. Open a bot after the guide to see this part on its page."
    }
  },
  "webui.bots.work": {
    "panel": "profiles",
    "targets": [
      "#botWork"
    ],
    "entryTargets": [
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst een bot in de lijst aan. Open na de uitleg een bot om dit onderdeel op zijn pagina te zien.",
      "en": "The highlight marks a bot in the list. Open a bot after the guide to see this part on its page."
    }
  },
  "webui.governance.waiting": {
    "panel": "approvals",
    "targets": [
      "#myWaitingSection"
    ]
  },
  "webui.projects.create": {
    "panel": "projects",
    "targets": [
      "#projNewName",
      "#projNewCategory",
      "#projCreateButton"
    ]
  },
  "webui.projects.team": {
    "panel": "projects",
    "targets": [
      "#projPeopleSearch",
      "#projCategory",
      "#projSaveTeam"
    ]
  },
  "webui.projects.chat": {
    "panel": "projects",
    "targets": [
      "#projStartChat"
    ]
  },
  "webui.projects.files": {
    "panel": "projects",
    "targets": [
      "#projUpload"
    ]
  },
  "webui.projects.archive": {
    "panel": "projects",
    "targets": [
      "#projArchiveTeam"
    ]
  },
  "webui.connections.providers": {
    "panel": "integrations",
    "targets": [
      "#integrationsRefreshBtn",
      "#intgSearch"
    ]
  },
  "webui.connections.channel": {
    "panel": "integrations",
    "targets": [
      ".intg-card[data-channel]"
    ]
  },
  "webui.connections.publish": {
    "panel": "integrations",
    "targets": [
      ".ch-public"
    ]
  },
  "webui.connections.people": {
    "panel": "integrations",
    "targets": [
      "#chAddPlatform",
      "#chAddUserId"
    ]
  },
  "webui.connections.personal": {
    "panel": "integrations",
    "targets": [
      "[data-mine-field]"
    ]
  },
  "webui.connections.link": {
    "panel": "integrations",
    "targets": [
      "#chMinePlatform",
      "#chMineOtherId"
    ]
  },
  "webui.connections.mcp": {
    "panel": "settings",
    "targets": [
      "#mcpServerList"
    ],
    "section": "system"
  },
  "webui.connections.catalog": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#mcpToolSearch"
    ]
  },
  "webui.governance.once": {
    "panel": "chat",
    "targets": [
      "#approvalBtnOnce"
    ]
  },
  "webui.governance.session": {
    "panel": "chat",
    "targets": [
      "#approvalBtnSession"
    ]
  },
  "webui.governance.always": {
    "panel": "chat",
    "targets": [
      "#approvalBtnAlways"
    ]
  },
  "webui.governance.deny": {
    "panel": "chat",
    "targets": [
      "#approvalCollapse",
      "#approvalBtnDeny"
    ]
  },
  "webui.governance.skip": {
    "panel": "chat",
    "targets": [
      "#approvalSkipAll",
      "#yoloPill"
    ]
  },
  "webui.governance.clarify": {
    "panel": "chat",
    "targets": [
      "#clarifyCollapse",
      "#clarifyInput",
      "#clarifySubmit"
    ]
  },
  "webui.governance.requests": {
    "panel": "approvals",
    "targets": [
      "#btnMyApprovalsRefresh"
    ]
  },
  "webui.governance.resume": {
    "panel": "settings",
    "targets": [
      "#accessRequestsList > label input[type=\"checkbox\"]",
      "#accessRequestsList [data-operation-id]"
    ],
    "section": "access"
  },
  "webui.governance.users": {
    "panel": "governance",
    "targets": [
      "#govUserEmail",
      "#govUserSave",
      "#govUserAccessLevel",
      "#govUserAccessMode",
      "#govUserApprovalMode"
    ]
  },
  "webui.governance.mcp_map": {
    "panel": "governance",
    "targets": [
      "#govUserMcpTools",
      "#govUserDenyMcpTools"
    ]
  },
  "webui.governance.groups": {
    "panel": "governance",
    "targets": [
      "#govGroupName",
      "#govGroupDesc",
      "#govGroupSso",
      "#govGroupRoles"
    ]
  },
  "webui.governance.preview": {
    "panel": "governance",
    "targets": [
      "#govPreviewEmail",
      "#govPreviewGroups"
    ]
  },
  "webui.governance.audit": {
    "panel": "governance",
    "targets": [
      "#governanceRefreshBtn"
    ]
  },
  "webui.governance.workspace_filter": {
    "panel": "governance",
    "targets": [
      "#govWsUserFilter"
    ]
  },
  "webui.governance.organisation": {
    "panel": "governance",
    "targets": []
  },
  "webui.operations.refresh": {
    "panel": "chat",
    "targets": [
      "#btnReload",
      "#btnStaleClientRefresh",
      "#offlineCheckNow"
    ]
  },
  "webui.operations.health": {
    "panel": "chat",
    "targets": [
      "#agentHealthDismiss",
      "#btnRestartGateway"
    ]
  },
  "webui.operations.terminal": {
    "panel": "chat",
    "targets": [
      "#btnTerminalClear",
      "#btnTerminalCopy",
      "#btnTerminalRestart",
      "#btnTerminalCollapse",
      "#btnTerminalClose",
      "#btnTerminalExpand",
      "#btnTerminalDockClose"
    ]
  },
  "webui.operations.logs": {
    "panel": "logs",
    "targets": [
      "#logsRefreshBtn",
      "#logsFile",
      "#logsTail",
      "#logsSeverityFilter",
      "#logsAutoRefresh",
      "#logsWrap",
      "#logsCopyAll"
    ]
  },
  "webui.operations.insights": {
    "panel": "insights",
    "targets": [
      "#insightsRefreshBtn",
      "#insightsPeriod"
    ]
  },
  "webui.operations.capacity": {
    "panel": "settings",
    "targets": [
      "#capacityPollSeconds",
      "#capacityCooldownSeconds",
      "#capacityThresholds",
      "#capacityDestination",
      "#btnCapacitySave"
    ],
    "section": "system"
  },
  "webui.operations.updates": {
    "panel": "settings",
    "targets": [
      "#btnCheckUpdatesNow"
    ],
    "section": "system"
  },
  "webui.operations.errors": {
    "panel": "chat",
    "targets": []
  },
  "webui.operations.extensions": {
    "panel": "settings",
    "targets": [
      "#extensionsCopyDiagnosticsBtn"
    ],
    "section": "extensions"
  },
  "webui.operations.dashboard": {
    "panel": "chat",
    "targets": [
      "#dashboardRailBtn",
      "#dashboardMobileBtn"
    ]
  },
  "webui.settings.theme": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#themePickerGrid"
    ]
  },
  "webui.settings.skin": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#skinPickerGrid"
    ]
  },
  "webui.settings.font_size": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#fontSizePickerGrid"
    ]
  },
  "webui.settings.workspace_panel_open": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsWorkspacePanelOpen"
    ]
  },
  "webui.settings.workspace_todos_tab": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsWorkspaceTodosTab"
    ]
  },
  "webui.settings.session_jump_buttons": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsSessionJumpButtons"
    ]
  },
  "webui.settings.session_endless_scroll": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsSessionEndlessScroll"
    ]
  },
  "webui.settings.chat_activity_display_mode": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsChatActivityDisplayMode"
    ]
  },
  "webui.settings.transparent_event_timestamps": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsTransparentEventTimestamps"
    ]
  },
  "webui.settings.auto_scroll_follow": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsAutoScrollFollow"
    ]
  },
  "webui.settings.render_user_markdown": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsRenderUserMarkdown"
    ]
  },
  "webui.settings.large_text_paste_as_attachment": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsLargeTextPasteAsAttachment"
    ]
  },
  "webui.settings.project_quick_create": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsProjectQuickCreate"
    ]
  },
  "webui.settings.structured_code_mode": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsStructuredCodeMode"
    ]
  },
  "webui.settings.structured_code_auto_lines": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsStructuredCodeAutoLines"
    ]
  },
  "webui.settings.show_titlebar_profile": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsShowTitlebarProfile"
    ]
  },
  "webui.settings.worklog_details_expanded_default": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsWorklogDetailsExpandedDefault"
    ]
  },
  "webui.settings.advanced_chat_controls": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      "#settingsAdvancedChatControls"
    ]
  },
  "webui.settings.model": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsModelChip",
      "#settingsModel"
    ]
  },
  "webui.settings.hide_suggestions": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsHideSuggestions"
    ]
  },
  "webui.settings.hide_empty_state_panel": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsHideEmptyStatePanel"
    ]
  },
  "webui.settings.virtualize_transcript": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsVirtualizeTranscript"
    ]
  },
  "webui.settings.send_key": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsSendKey"
    ]
  },
  "webui.settings.language": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsLanguage"
    ]
  },
  "webui.settings.rtl": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsRtl"
    ]
  },
  "webui.settings.sound_enabled": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsSoundEnabled"
    ]
  },
  "webui.settings.tts_enabled": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTtsEnabled"
    ]
  },
  "webui.settings.tts_auto_read": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTtsAutoRead"
    ]
  },
  "webui.settings.voice_mode_enabled": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsVoiceModeEnabled"
    ]
  },
  "webui.settings.raw_audio": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsRawAudio"
    ]
  },
  "webui.settings.dictation_append": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsDictationAppend"
    ]
  },
  "webui.settings.tts_engine": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTtsEngine"
    ]
  },
  "webui.settings.tts_voice": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTtsVoice"
    ]
  },
  "webui.settings.tts_rate": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTtsRate"
    ]
  },
  "webui.settings.tts_pitch": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTtsPitch"
    ]
  },
  "webui.settings.notifications_enabled": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsNotificationsEnabled"
    ]
  },
  "webui.settings.show_token_usage": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowTokenUsage"
    ]
  },
  "webui.settings.max_tokens": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsMaxTokens"
    ]
  },
  "webui.settings.show_quota_chip": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowQuotaChip"
    ]
  },
  "webui.settings.show_conversation_outline": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowConversationOutline"
    ]
  },
  "webui.settings.show_tps": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowTps"
    ]
  },
  "webui.settings.fade_text_effect": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsFadeTextEffect"
    ]
  },
  "webui.settings.terminal_auto_expand": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsTerminalAutoExpand"
    ]
  },
  "webui.settings.api_redact": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsApiRedact"
    ]
  },
  "webui.settings.sidebar_density": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsSidebarDensity"
    ]
  },
  "webui.settings.pinned_sessions_limit": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsPinnedSessionsLimit"
    ]
  },
  "webui.settings.auto_title_refresh": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsAutoTitleRefresh"
    ]
  },
  "webui.settings.default_message_mode": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsDefaultMessageMode"
    ]
  },
  "webui.settings.show_busy_placeholder_hint": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowBusyPlaceholderHint"
    ]
  },
  "webui.settings.new_chat_on_workspace_switch": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsNewChatOnWorkspaceSwitch"
    ]
  },
  "webui.settings.show_cli_sessions": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowCliSessions"
    ]
  },
  "webui.settings.show_cron_sessions": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowCronSessions"
    ]
  },
  "webui.settings.show_claude_code_sessions": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowClaudeCodeSessions"
    ]
  },
  "webui.settings.show_webhook_sessions": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowWebhookSessions"
    ]
  },
  "webui.settings.show_previous_messaging_sessions": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsShowPreviousMessagingSessions"
    ]
  },
  "webui.settings.sync_insights": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsSyncInsights"
    ]
  },
  "webui.settings.check_updates": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsCheckUpdates"
    ]
  },
  "webui.settings.update_channel": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsUpdateChannel"
    ]
  },
  "webui.settings.ignore_agent_updates": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsIgnoreAgentUpdates"
    ]
  },
  "webui.settings.whats_new_summary": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsWhatsNewSummary"
    ]
  },
  "webui.settings.bot_name": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#settingsBotName"
    ]
  },
  "webui.settings.dashboard_mode": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#settingsDashboardMode"
    ]
  },
  "webui.settings.dashboard_url": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#settingsDashboardUrl"
    ]
  },
  "webui.settings.save": {
    "panel": "settings",
    "targets": [
      "#settingsPanePreferences [data-i18n='settings_save_btn']"
    ],
    "section": "preferences"
  },
  "webui.settings.photo": {
    "panel": "settings",
    "section": "appearance",
    "targets": [
      ".profile-photo-upload",
      "#profilePhotoRemoveBtn"
    ]
  },
  "webui.settings.interaction": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#interactionUserMode",
      "#interactionTaskMode",
      "#interactionSave",
      "#interactionReload"
    ]
  },
  "webui.settings.aux_models": {
    "panel": "settings",
    "targets": [
      "#btnResetAuxModels",
      "#btnApplyAuxModels"
    ],
    "section": "preferences"
  },
  "webui.settings.advanced_model": {
    "panel": "settings",
    "targets": [
      "#mainAdvancedBtn",
      "#auxAdvancedClose",
      "#auxAdvancedCancel",
      "#auxAdvancedSave",
      "#auxAdvancedServiceTier",
      "#auxAdvancedExtraBody",
      "#auxAdvancedApiKeyClear"
    ],
    "section": "preferences"
  },
  "webui.settings.notification_permission": {
    "panel": "settings",
    "section": "preferences",
    "targets": [
      "#notificationPermissionButton"
    ]
  },
  "webui.settings.password": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#settingsPassword",
      "#settingsCurrentPassword"
    ]
  },
  "webui.settings.passkey": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#btnRegisterPasskey"
    ]
  },
  "webui.settings.passwordless": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#btnGoPasswordless"
    ]
  },
  "webui.settings.disable_auth": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#settingsAuthDisabledAck",
      "#btnDisableAuth"
    ]
  },
  "webui.settings.signout": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#btnSignOut"
    ]
  },
  "webui.settings.shutdown": {
    "panel": "settings",
    "section": "system",
    "targets": [
      "#btnShutdownServer"
    ]
  },
  "webui.settings.composer_visibility": {
    "panel": "settings",
    "targets": [
      "#composerControlsChips",
      "#composerSituationalControlsChips"
    ],
    "section": "appearance"
  },
  "webui.settings.navigation": {
    "panel": "settings",
    "targets": [
      "#tabVisibilityChips"
    ],
    "section": "appearance"
  },
  "webui.connections.onboarding": {
    "panel": "chat",
    "targets": [
      "#onboardingBackBtn",
      "#onboardingSkipBtn",
      "#onboardingNextBtn",
      "#onboardingBaseUrlInput",
      "#onboardingApiKeyInput",
      "#onboardingModelInput",
      "#onboardingModelSelect",
      "#onboardingProviderSelect",
      "#onboardingWorkspaceSelect",
      "#onboardingWorkspaceInput",
      "#onboardingPasswordInput"
    ]
  },
  "webui.connections.oauth": {
    "panel": "settings",
    "targets": [
      "#anthropicOAuthBtn",
      "#codexOAuthBtn"
    ],
    "section": "providers"
  },
  "webui.planned.gateway": {
    "panel": "settings",
    "section": "system",
    "targets": []
  },
  "webui.planned.reporter": {
    "panel": "settings",
    "section": "system",
    "targets": []
  },
  "webui.planned.central_memory": {
    "panel": "settings",
    "section": "system",
    "targets": []
  },
  "webui.sessions.rename": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.sessions.pin": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.sessions.move_project": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.sessions.archive": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.sessions.delete": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.sessions.regenerate_title": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.sessions.link": {
    "panel": "chat",
    "targets": [],
    "entryTargets": [
      ".session-actions-trigger",
      "#btnHamburger",
      ".rail [data-panel='chat']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar gespreksacties aan. Open na de uitleg de chatzijbalk en het driepuntjesmenu bij een gesprek; kies daar de beschreven actie.",
      "en": "The highlight marks the entry to conversation actions. After the guide, open the chat sidebar and the three-dot menu beside a conversation, then choose the described action."
    }
  },
  "webui.chat.save_prompt": {
    "panel": "chat",
    "targets": [
      "#btnSavedPrompts"
    ]
  },
  "webui.bots.knowledge_upload": {
    "panel": "profiles",
    "targets": [
      "[data-knowledge-upload]"
    ],
    "entryTargets": [
      "[data-builder-tab='4']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.bots.knowledge_legacy": {
    "panel": "profiles",
    "targets": [
      "#builderKnowledge details"
    ],
    "entryTargets": [
      "[data-builder-tab='4']",
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar deze botinstellingen aan. Open na de uitleg de gewenste bot, kies de beschreven configuratieknop en vervolgens het bijbehorende tabblad. Je wijzigingen worden pas met Opslaan bevestigd.",
      "en": "The highlight marks the entry to these bot settings. After the guide, open the intended bot, choose the described configuration button, then its matching tab. Changes are confirmed only with Save."
    }
  },
  "webui.memory.overview": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.today": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.visualiser": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.review": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.profile": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.lifecycle": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.graph": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.triples": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.recall": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.timeline": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.consolidations": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.structured_overview": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.facts": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.structured_timelines": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.instructions": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.structured_graph": {
    "panel": "memory",
    "targets": []
  },
  "webui.memory.preferences": {
    "panel": "memory",
    "targets": []
  },
  "webui.settings.search": {
    "panel": "settings",
    "targets": [
      "#settingsSearch"
    ],
    "section": "preferences"
  },
  "webui.files.hidden": {
    "panel": "chat",
    "targets": [
      "#workspaceShowHiddenFiles"
    ],
    "entryTargets": [
      "#btnWorkspacePrefs",
      "#btnWorkspacePanelToggle",
      "#btnWorkspacePanelEdgeToggle",
      ".rail [data-panel='files']"
    ],
    "entryHint": {
      "nl": "De markering wijst de ingang naar bestandsopties aan. Open na de uitleg het bestandenpaneel en het driepuntjesmenu Werkruimteopties; daar staat Verborgen bestanden tonen.",
      "en": "The highlight marks the entry to file options. After the guide, open the files panel and its three-dot Workspace options menu to find Show hidden files."
    }
  },
  "webui.governance.approval_prompt": {
    "panel": "governance",
    "targets": [
      "#govUserApprovalPrompt"
    ]
  },
  "webui.governance.effective_grants": {
    "panel": "governance",
    "targets": [
      "#govUserEffective"
    ]
  },
  "webui.governance.group_template": {
    "panel": "governance",
    "targets": [
      ".gov-chip-role"
    ]
  },
  "webui.governance.integration_approval": {
    "panel": "governance",
    "targets": []
  },
  "webui.operations.extension_gallery": {
    "panel": "settings",
    "targets": [
      "#extensionsGallery"
    ],
    "section": "extensions"
  },
  "webui.operations.extension_installed": {
    "panel": "settings",
    "targets": [
      "#extensionsInstalled"
    ],
    "section": "extensions"
  },
  "webui.chat.slash": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.goal": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.queue": {
    "panel": "chat",
    "targets": [
      "#queuePill",
      "#queueCard",
      "#msg"
    ]
  },
  "webui.chat.interrupt": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.steer": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.aside": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.background": {
    "panel": "chat",
    "targets": [
      "#bgBadge",
      "#msg"
    ]
  },
  "webui.chat.status": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.chat.personality": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.skills.use": {
    "panel": "chat",
    "targets": [
      "#msg"
    ]
  },
  "webui.modules.apps": {
    "panel": "apps",
    "targets": [
      "#mainApps"
    ]
  },
  "webui.modules.overview": {
    "panel": "modules",
    "targets": [
      "#mainModules .modules-intro"
    ]
  },
  "webui.modules.budget": {
    "panel": "modules",
    "targets": [
      "#mainModules .module-meta"
    ]
  },
  "webui.modules.connections": {
    "panel": "modules",
    "targets": [
      "#mainModules .module-card details:has(ul) > summary"
    ]
  },
  "webui.modules.request": {
    "panel": "modules",
    "targets": [
      "#mainModules .module-card details:not(:has(ul)) > summary"
    ]
  },
  "webui.modules.language": {
    "panel": "modules",
    "targets": [
      "#mainModules .module-language select"
    ]
  },
  "webui.apps.overview": {
    "panel": "apps",
    "targets": [
      "#mainApps"
    ]
  },
  "webui.apps.search": {
    "panel": "apps",
    "targets": [
      "#appsSearch"
    ]
  },
  "webui.apps.categories": {
    "panel": "apps",
    "targets": [
      "#appsCategory"
    ]
  },
  "webui.apps.opendesign": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"opendesign\"]"
    ]
  },
  "webui.apps.craft_studio": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"craft_studio\"]"
    ]
  },
  "webui.apps.photocraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"photocraft\"]"
    ]
  },
  "webui.apps.filmcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"filmcraft\"]"
    ]
  },
  "webui.apps.effectcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"effectcraft\"]"
    ]
  },
  "webui.apps.vectorcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"vectorcraft\"]"
    ]
  },
  "webui.apps.lightcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"lightcraft\"]"
    ]
  },
  "webui.apps.designcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"designcraft\"]"
    ]
  },
  "webui.apps.pdfcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"pdfcraft\"]"
    ]
  },
  "webui.apps.soundcraft": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"soundcraft\"]"
    ]
  },
  "webui.apps.hyperframes": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"hyperframes\"]"
    ]
  },
  "webui.apps.blender": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"blender\"]"
    ]
  },
  "webui.apps.openseo": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"openseo\"]"
    ]
  },
  "webui.apps.twenty": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"twenty\"]"
    ]
  },
  "webui.apps.n8n": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"n8n\"]"
    ]
  },
  "webui.apps.omniroute": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"omniroute\"]"
    ]
  },
  "webui.apps.nango": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"nango\"]"
    ]
  },
  "webui.apps.docuseal": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"docuseal\"]"
    ]
  },
  "webui.apps.language": {
    "panel": "apps",
    "targets": [
      "#appsLanguage"
    ]
  },
  "webui.apps.ragflow": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"ragflow\"]"
    ]
  },
  "webui.apps.langfuse": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"langfuse\"]"
    ]
  },
  "webui.apps.hermes_docs": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"hermes_docs\"]"
    ]
  },
  "webui.apps.hermes_sheets": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"hermes_sheets\"]"
    ]
  },
  "webui.apps.hermes_slides": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"hermes_slides\"]"
    ]
  },
  "webui.apps.hermes_pdf": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"hermes_pdf\"]"
    ]
  },
  "webui.apps.hermes_markdown": {
    "panel": "apps",
    "targets": [
      "#mainApps [data-app-id=\"hermes_markdown\"]"
    ]
  },
  "webui.msp.overview": {
    "panel": "msp",
    "targets": [
      "#mspOverviewTab"
    ]
  },
  "webui.msp.tickets": {
    "panel": "msp",
    "targets": [
      "#mspTicketsTab"
    ]
  },
  "webui.msp.billing": {
    "panel": "msp",
    "targets": [
      "#mspBillingTab"
    ]
  },
  "webui.apps.availability": {
    "panel": "apps",
    "targets": [],
    "entryTargets": [
      "#mainApps"
    ],
    "entryHint": {
      "nl": "Zoek de gewenste app op deze pagina. De beschikbare bediening hangt af van je rechten en de app-koppeling.",
      "en": "Find the app on this page. Available controls depend on your permissions and the app connection."
    }
  },
  "webui.apps.support": {
    "panel": "apps",
    "targets": [
      "#mainApps .app-support"
    ],
    "entryTargets": [
      "#mainApps"
    ],
    "entryHint": {
      "nl": "Zoek de gewenste app op deze pagina. De beschikbare bediening hangt af van je rechten en de app-koppeling.",
      "en": "Find the app on this page. Available controls depend on your permissions and the app connection."
    }
  },
  "webui.apps.request": {
    "panel": "apps",
    "targets": [],
    "entryTargets": [
      "#mainApps"
    ],
    "entryHint": {
      "nl": "Zoek de gewenste app op deze pagina. De beschikbare bediening hangt af van je rechten en de app-koppeling.",
      "en": "Find the app on this page. Available controls depend on your permissions and the app connection."
    }
  },
  "webui.msp.package": {
    "panel": "msp",
    "targets": [
      "#mspBasePackage"
    ],
    "entryTargets": [
      "#mspOverviewTab"
    ],
    "entryHint": {
      "nl": "Kies Overzicht om het basispakket en je consultantinzet te bekijken.",
      "en": "Choose Overview to see your base package and consultant capacity."
    }
  },
  "webui.msp.consultant": {
    "panel": "msp",
    "targets": [
      "#mspConsultant"
    ],
    "entryTargets": [
      "#mspOverviewTab"
    ],
    "entryHint": {
      "nl": "Kies Overzicht om het basispakket en je consultantinzet te bekijken.",
      "en": "Choose Overview to see your base package and consultant capacity."
    }
  },
  "webui.msp.job-costs": {
    "panel": "msp",
    "targets": [
      "#mspJobCosts"
    ],
    "entryTargets": [
      "#mspOverviewTab"
    ],
    "entryHint": {
      "nl": "Kies Overzicht. Uren invoeren doe je bij de betreffende scheduled task.",
      "en": "Choose Overview. Enter hours on the scheduled task itself."
    }
  },
  "webui.msp.annual-support": {
    "panel": "msp",
    "targets": [
      "#mspAnnualSupport"
    ],
    "entryTargets": [
      "#mspOverviewTab"
    ],
    "entryHint": {
      "nl": "Kies Overzicht.",
      "en": "Choose Overview."
    }
  },
  "webui.connections.app_connectors": {
    "panel": "integrations",
    "targets": [
      "#intgTabConnectors",
      "#intgSearch"
    ]
  },
  "webui.connections.plugins": {
    "panel": "integrations",
    "targets": [
      "#intgTabPlugins",
      "#pluginCatalogSearch"
    ]
  },
  "webui.connections.vault": {
    "panel": "integrations",
    "targets": [
      "#intgTabVault",
      "#vaultSources"
    ]
  },
  "webui.bots.scheduled_tasks": {
    "panel": "profiles",
    "targets": [
      "#builderAssignTask",
      "#builderCreateTask"
    ],
    "entryTargets": [
      ".profile-card[data-name]"
    ],
    "entryHint": {
      "en": "Open the relevant section after closing the guide. The guide does not activate controls or save changes.",
      "nl": "Open het bijbehorende onderdeel na het sluiten van de gids. De gids bedient geen knoppen en slaat geen wijzigingen op."
    }
  },
  "webui.studio.usecases": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-usecases']",
      "#studioHubNav"
    ]
  },
  "webui.studio.overview": {
    "panel": "studio",
    "targets": [
      ".wp-bar",
      "#studioHubNav"
    ]
  },
  "webui.studio.discover": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-opportunities']",
      "[data-tour='studio-steps']"
    ]
  },
  "webui.studio.design": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-baseline']"
    ]
  },
  "webui.studio.plan": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-plan']"
    ]
  },
  "webui.studio.build": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-build']"
    ]
  },
  "webui.studio.conversation": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-conversation']"
    ]
  },
  "webui.studio.backlog": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-backlog']",
      "[data-tour='studio-backlog-add']"
    ]
  },
  "webui.studio.playbooks": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-playbooks']",
      "[data-tour='studio-playbook-new']"
    ]
  },
  "webui.studio.playbook_edit": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-playbook-steps']"
    ]
  },
  "webui.studio.playbook_run": {
    "panel": "studio",
    "targets": [
      "[data-pb-run]"
    ]
  },
  "webui.org.chart": {
    "panel": "organisation",
    "targets": [
      "[data-tour='org-totals']",
      "[data-tour='org-chart']"
    ]
  },
  "webui.org.numbers": {
    "panel": "organisation",
    "targets": [
      "[data-tour='org-detail']",
      ".om-mini-stat"
    ]
  },
  "webui.org.team": {
    "panel": "organisation",
    "targets": [
      "[data-tour='org-team']"
    ],
    "entryTargets": [
      ".om-team-open"
    ],
    "entryHint": {
      "nl": "Kies Details bij een team om dit paneel te openen.",
      "en": "Choose Details on a team to open this panel."
    }
  },
  "webui.org.teams": {
    "panel": "organisation",
    "targets": [
      "[data-tour='org-teams']"
    ]
  },
  "webui.mgmt.overview": {
    "panel": "management",
    "targets": [
      "[data-tour='mgmt-kpis']",
      "[data-tour='mgmt-decisions']"
    ]
  },
  "webui.mgmt.tables": {
    "panel": "management",
    "targets": [
      "[data-tour='mgmt-teams']",
      "[data-tour='mgmt-bots']"
    ]
  },
  "webui.mgmt.pricing": {
    "panel": "management",
    "targets": [
      "#omSettings"
    ],
    "entryTargets": [
      "[data-om-settings]"
    ],
    "entryHint": {
      "nl": "Kies Prijzen en tarieven in de kop van Management.",
      "en": "Choose Prices and rates in the Management header."
    }
  },
  "webui.value.task": {
    "panel": "tasks",
    "targets": [
      "#omValueCard"
    ],
    "entryTargets": [
      "#cronFormValueRow"
    ],
    "entryHint": {
      "nl": "De waarde vul je in bij het maken van een taak, of bij Waarde in de details van een taak.",
      "en": "Fill in the value when creating a task, or under Value in a task's details."
    }
  },
  "webui.value.bot_duty": {
    "panel": "profiles",
    "targets": [
      "[data-tour='bot-duty-value']",
      "#botServes"
    ]
  },
  "webui.value.chat_chip": {
    "panel": "chat",
    "targets": [
      "#omChatValue"
    ]
  },
  "webui.studio.prompts": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-prompts']",
      "[data-tour='studio-prompt-new']"
    ]
  },
  "webui.studio.org_profile": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-profile']",
      "[data-tour='studio-profile-research']"
    ]
  },
  "webui.studio.intake_me": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-intake']",
      "[data-tour='studio-intake-tasks']",
      "[data-tour='studio-intake-note']"
    ]
  },
  "webui.studio.intake_org": {
    "panel": "studio",
    "targets": [
      "[data-tour='studio-intake']",
      "[data-tour='studio-intake-rules']"
    ]
  }
};
  for (const entry of Object.values(entries)) {
    Object.freeze(entry.targets);
    if (entry.entryTargets) Object.freeze(entry.entryTargets);
    if (entry.entryHint) Object.freeze(entry.entryHint);
    Object.freeze(entry);
  }
  window.SynthPulseFeatureTargets = Object.freeze(entries);
})();
