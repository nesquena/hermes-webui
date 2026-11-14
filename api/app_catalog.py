"""Audited app identities. Deployment URLs belong in SP_APP_LINKS, never here."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AppSpec:
    id: str
    name: str
    description_nl: str
    description_en: str
    module: str = 'design_studio'
    category: str = 'creative'
    access: str = 'member'
    requestable: bool = True


_ROWS = (
    AppSpec('opendesign', 'OpenDesign',
            'Ontwerp en bewerk visuele projecten en merkuitingen.',
            'Design and edit visual projects and branded materials.'),
    AppSpec('craft_studio', 'Craft Studio',
            'Kies een creatieve editor voor beeld, video, ontwerp, documenten of audio.',
            'Choose a creative editor for images, video, design, documents or audio.'),
    AppSpec('photocraft', 'PhotoCraft', 'Bewerk foto’s en maak beeldcomposities.',
            'Edit photos and create image compositions.'),
    AppSpec('filmcraft', 'FilmCraft', 'Monteer video’s en werk aan je tijdlijn.',
            'Edit videos and work on your timeline.'),
    AppSpec('effectcraft', 'EffectCraft', 'Maak visuele effecten en bewegende composities.',
            'Create visual effects and animated compositions.'),
    AppSpec('vectorcraft', 'VectorCraft', 'Teken en bewerk vectorillustraties.',
            'Draw and edit vector illustrations.'),
    AppSpec('lightcraft', 'LightCraft', 'Ontwikkel en corrigeer foto’s.',
            'Develop and correct photos.'),
    AppSpec('designcraft', 'DesignCraft', 'Maak ontwerpen en paginalay-outs.',
            'Create designs and page layouts.'),
    AppSpec('pdfcraft', 'PDFCraft', 'Open en bewerk pdf-documenten.',
            'Open and edit PDF documents.'),
    AppSpec('soundcraft', 'SoundCraft', 'Neem audio op en bewerk geluid.',
            'Record audio and edit sound.'),
    AppSpec('hyperframes', 'HyperFrames', 'Maak en bekijk animaties en videocomposities.',
            'Create and preview animations and video compositions.'),
    AppSpec('blender', 'Blender', 'Maak 3D-modellen, scènes en renders in Blender in je browser.',
            'Create 3D models, scenes and renders in Blender in your browser.'),
    AppSpec('openseo', 'OpenSEO', 'Doe zoekwoordonderzoek, volg posities en audit websites voor SEO en AI-zichtbaarheid.',
            'Research keywords, track rankings and audit websites for SEO and AI visibility.', 'workspace', 'marketing', 'platform'),
    AppSpec('twenty', 'Twenty CRM', 'Beheer contacten, bedrijven, deals, notities en taken in het CRM.',
            'Manage contacts, companies, deals, notes and tasks in the CRM.', 'workspace', 'sales', 'platform'),
    AppSpec('n8n', 'n8n', 'Bouw en beheer geautomatiseerde workflows.',
            'Build and manage automated workflows.', 'workflows', 'automation', 'platform'),
    AppSpec('omniroute', 'OmniRoute', 'Beheer modelroutes en verbindingen met AI-aanbieders.',
            'Manage model routing and AI provider connections.', 'workspace', 'platform', 'platform', requestable=False),
    AppSpec('nango', 'App-connectors', 'Beheer integraties en verbindingen met externe diensten.',
            'Manage integrations and connections to external services.', 'workspace', 'platform', 'platform', requestable=False),
    AppSpec('docuseal', 'DocuSeal', 'Bereid documenten voor en volg digitale ondertekening.',
            'Prepare documents and follow digital signing.', 'e_signing', 'productivity'),
    AppSpec('ragflow', 'RAGFlow', 'Beheer kennisbanken, verwerk documenten en test antwoorden met bronverwijzingen.',
            'Manage knowledge bases, process documents and test answers with source references.', 'knowledge_base', 'productivity', 'platform'),
    AppSpec('langfuse', 'Langfuse', 'Bekijk AI-traces, gebruik, kosten en evaluaties van gekoppelde toepassingen.',
            'Inspect AI traces, usage, costs and evaluations from connected applications.', 'workspace', 'platform', 'platform', requestable=False),
    AppSpec('hermes_docs', 'SynthPulse Office Docs (Word)', 'Schrijf en bewerk Word-documenten met opmaak, tabellen en AI-hulp.',
            'Write and edit Word documents with formatting, tables and AI assistance.', 'office_apps', 'productivity', 'platform'),
    AppSpec('hermes_sheets', 'SynthPulse Office Sheets (Excel)', 'Bewerk Excel-werkboeken met cellen, formules en AI-hulp.',
            'Edit Excel workbooks with cells, formulas and AI assistance.', 'office_apps', 'productivity', 'platform'),
    AppSpec('hermes_slides', 'SynthPulse Office Slides (PowerPoint)', 'Maak en bewerk PowerPoint-presentaties met dia’s en AI-hulp.',
            'Create and edit PowerPoint presentations with slides and AI assistance.', 'office_apps', 'productivity', 'platform'),
    AppSpec('hermes_pdf', 'SynthPulse Office PDF', 'Open pdf-documenten in je AI-omgeving en gebruik de beschikbare annotatie- en AI-functies.',
            'Open PDF documents in your AI environment and use the available annotation and AI features.', 'office_apps', 'productivity', 'platform'),
    AppSpec('hermes_markdown', 'SynthPulse Office Markdown', 'Schrijf en bewerk Markdown met een teksteditor en AI-hulp.',
            'Write and edit Markdown with a text editor and AI assistance.', 'office_apps', 'productivity', 'platform'),
)

REGISTRY = {app.id: app for app in _ROWS}
# A configured named app replaces only its matching legacy launcher entry.
LEGACY_REPLACEMENTS = {'design_studio': ('opendesign',), 'workflows': ('n8n',),
                       'e_signing': ('docuseal',)}
