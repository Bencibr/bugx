/**
 * BugX Project Settings & Inspector Floating Modal
 * Interacts with GET/POST /projects/:hash/settings and GET/POST /project/settings
 * Provides full management of BugX's 5 Standard Startup & Lifecycle Gates (DOC-167/DES-11)
 */

(function () {
  let currentHash = '';
  let projectsList = [];
  let currentSettings = null;

  // Clean, crisp SVG Icon definitions (Apple HIG / modern IDE aesthetic)
  const BxIcons = {
    trash: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4h12M5.3 4V2.7a1.3 1.3 0 011.4-1.4h2.6a1.3 1.3 0 011.4 1.4V4m2 0v9.3a1.3 1.3 0 01-1.3 1.4H4.7a1.3 1.3 0 01-1.4-1.4V4h9.4zM6.7 7.3v4M9.3 7.3v4"/></svg>`,
    folder: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M1.75 4c0-.55.45-1 1-1h3l1.5 1.5h5c.55 0 1 .45 1 1V12c0 .55-.45 1-1 1h-10c-.55 0-1-.45-1-1V4z"/></svg>`,
    folderOpen: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3.5a1 1 0 011-1h3.25l1.5 1.5H13a1 1 0 011 1v1.5H2v-3z"/><path d="M1.5 6.5h13l-1.4 6.8a1 1 0 01-.98.7H3.88a1 1 0 01-.98-.7L1.5 6.5z"/></svg>`,
    folderPlus: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M1.75 4c0-.55.45-1 1-1h3l1.5 1.5h5c.55 0 1 .45 1 1V7M1.75 4v8c0 .55.45 1 1 1h5.25M11.5 10v6M8.5 13h6"/></svg>`,
    files: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3.5a1 1 0 011-1h3.25l1.5 1.5H13a1 1 0 011 1v1.5H2v-3z"/><path d="M1.5 6.5h13l-1.4 6.8a1 1 0 01-.98.7H3.88a1 1 0 01-.98-.7L1.5 6.5z"/></svg>`,
    file: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2.5a1 1 0 011-1h5.5L13 5v8.5a1 1 0 01-1 1H4a1 1 0 01-1-1v-11z"/><path d="M9 1.5V5h3.5"/></svg>`,
    fileCode: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 2.5a1 1 0 011-1h5.5L13 5v8.5a1 1 0 01-1 1H4a1 1 0 01-1-1v-11z"/><path d="M5.5 8.5L4 10l1.5 1.5M10.5 8.5L12 10l-1.5 1.5M9 1.5V5h3.5"/></svg>`,
    settings: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="2.5"/><path d="M13.8 6.8l-.8-.5a5.4 5.4 0 00-.6-1.5l.4-.9a.75.75 0 00-.2-.9l-1.1-1.1a.75.75 0 00-.9-.2l-.9.4c-.5-.2-1-.4-1.5-.6l-.5-.8a.75.75 0 00-.8-.5h-1.6a.75.75 0 00-.7.5l-.5.8c-.5.2-1 .4-1.5.6l-.9-.4a.75.75 0 00-.9.2L1.8 3.5a.75.75 0 00-.2.9l.4.9c-.2.5-.4 1-.6 1.5l-.8.5a.75.75 0 00-.5.8v1.6c0 .3.2.6.5.7l.8.5c.2.5.4 1 .6 1.5l-.4.9a.75.75 0 00.2.9l1.1 1.1c.2.2.6.3.9.2l.9-.4c.5.2 1 .4 1.5.6l.5.8c.2.3.5.5.8.5h1.6c.3 0 .6-.2.7-.5l.5-.8c.5-.2 1-.4 1.5-.6l.9.4a.75.75 0 00.9-.2l1.1-1.1a.75.75 0 00.2-.9l-.4-.9c.2-.5.4-1 .6-1.5l.8-.5a.75.75 0 00.5-.7V7.6a.75.75 0 00-.5-.8z"/></svg>`,
    copy: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3a1.5 1.5 0 00-1.5-1.5H3A1.5 1.5 0 001.5 3v6A1.5 1.5 0 003 10.5h2.5"/></svg>`,
    ide: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4a1 1 0 01-1 1H3a1 1 0 01-1-1V5a1 1 0 011-1h4M9.5 2h4.5v4.5M14 2L7 9"/></svg>`,
    home: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 6.5l6-4.5 6 4.5v7a1 1 0 01-1 1H3a1 1 0 01-1-1v-7z"/><path d="M6 14.5v-5h4v5"/></svg>`,
    plus: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3.5v9M3.5 8h9"/></svg>`,
    search: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>`,
    levelUp: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5L2.5 7 6 10.5M3 7h7a3 3 0 013 3v2.5"/></svg>`,
    refresh: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 2.5v3.5h-3.5M2.5 13.5v-3.5h3.5"/><path d="M3.2 6.5a5.5 5.5 0 019.2-1.7l1.1 1.2M12.8 9.5a5.5 5.5 0 01-9.2 1.7l-1.1-1.2"/></svg>`,
    shield: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5l5 2v4.5c0 3.5-2.5 6.5-5 7.5-2.5-1-5-4-5-7.5V3.5l5-2z"/></svg>`,
    bolt: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 1.5L2.5 9h5l-1 5.5 7-7.5h-5z"/></svg>`,
    compass: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><polygon points="10.5 5.5 9 9 5.5 10.5 7 7"/></svg>`,
    plug: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 1.5v3M10 1.5v3M4 4.5h8v4a4 4 0 01-4 4 4 4 0 01-4-4v-4zM8 12.5v2"/></svg>`,
    book: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3a1.5 1.5 0 011.5-1.5h3.5a1.5 1.5 0 011.5 1.5v10a1.5 1.5 0 00-1.5-1.5H4A1.5 1.5 0 002.5 13V3zM13.5 3a1.5 1.5 0 00-1.5-1.5H8.5a1.5 1.5 0 00-1.5 1.5v10a1.5 1.5 0 011.5-1.5h4a1.5 1.5 0 011.5 1.5V3z"/></svg>`,
    archive: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2.5" width="12" height="3" rx="0.5"/><path d="M3 5.5v7.5a1 1 0 001 1h8a1 1 0 001-1V5.5M6.5 8.5h3"/></svg>`,
    launch: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2h4v4M14 2L8 8M6 3H3a1 1 0 00-1 1v9a1 1 0 001 1h9a1 1 0 001-1v-3"/></svg>`,
    check: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5l3 3 6.5-7"/></svg>`,
    chevronRight: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5l4.5 4.5L6 12.5"/></svg>`,
    moreHorizontal: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="currentColor"><circle cx="3" cy="8" r="1.35"/><circle cx="8" cy="8" r="1.35"/><circle cx="13" cy="8" r="1.35"/></svg>`,
    finder: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="1.5" y="2" width="13" height="12" rx="2.5"/><path d="M5.5 5.5v.5M10.5 5.5v.5M5.5 10c1.2 1.2 3.8 1.2 5 0"/><path d="M8 2v6.5"/></svg>`,
    edit: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M11 2a1.4 1.4 0 012 2L4.5 12.5 1.5 13.5l1-3L11 2z"/></svg>`,
    radar: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><circle cx="8" cy="8" r="4"/><circle cx="8" cy="8" r="1.5"/><path d="M8 1.5v13M1.5 8h13M8 8l4.5-4.5"/></svg>`,
    barChart: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 13.5h12M4 11V7M8 11V3.5M12 11V5"/></svg>`,
    calculator: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="1.5" width="11" height="13" rx="1.5"/><line x1="4.5" y1="4.5" x2="11.5" y2="4.5"/><circle cx="5.5" cy="7.5" r="0.6" fill="currentColor"/><circle cx="8" cy="7.5" r="0.6" fill="currentColor"/><circle cx="10.5" cy="7.5" r="0.6" fill="currentColor"/><circle cx="5.5" cy="10" r="0.6" fill="currentColor"/><circle cx="8" cy="10" r="0.6" fill="currentColor"/><circle cx="10.5" cy="10" r="0.6" fill="currentColor"/><circle cx="5.5" cy="12" r="0.6" fill="currentColor"/><circle cx="8" cy="12" r="0.6" fill="currentColor"/><circle cx="10.5" cy="12" r="0.6" fill="currentColor"/></svg>`,
    pulse: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="1.5 8.5 4.5 8.5 6.5 3 9.5 13.5 11.5 8.5 14.5 8.5"/></svg>`,
    code: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="5 5 2 8 5 11"/><polyline points="11 5 14 8 11 11"/></svg>`,
    modeSwitch: (s = 13, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 3.5L2.5 6.5L5.5 9.5"/><path d="M2.5 6.5H13.5"/><path d="M10.5 12.5L13.5 9.5L10.5 6.5"/><path d="M13.5 9.5H2.5"/></svg>`,
    modeBadge: (s = 13) => `<span class="bx-mode-badge-wrap" style="display:inline-flex;align-items:center;justify-content:center;width:${s+6}px;height:${s+6}px;border-radius:5px;background:rgba(2,132,199,0.12);color:#0284c7;flex-shrink:0;"><svg width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 3.5L2.5 6.5L5.5 9.5"/><path d="M2.5 6.5H13.5"/><path d="M10.5 12.5L13.5 9.5L10.5 6.5"/><path d="M13.5 9.5H2.5"/></svg></span>`,
    bug: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="8" height="7" rx="4"/><path d="M8 2v3M8 12v2.5M2.5 6.5L4 7.5M13.5 6.5L12 7.5M2.5 9.5h2M11.5 9.5h2M3 12.5l1.5-1M13 12.5l-1.5-1M6 2.5l-.5-.5M10 2.5l.5-.5"/></svg>`,
    calendar: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3.5" width="12" height="10.5" rx="1.5"/><path d="M5 2v3M11 2v3M2 6.5h12"/></svg>`,
    trendingUp: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="14 4.5 9 9.5 6 6.5 2 10.5"/><polyline points="10 4.5 14 4.5 14 8.5"/></svg>`,
    topology: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="1.5" width="4" height="3" rx="0.8"/><rect x="1.5" y="11.5" width="4" height="3" rx="0.8"/><rect x="10.5" y="11.5" width="4" height="3" rx="0.8"/><path d="M8 4.5v3.5M3.5 11.5V9.5a1.5 1.5 0 011.5-1.5h6a1.5 1.5 0 011.5 1.5v2"/></svg>`,
    package: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L14 4.5v7l-6 3-6-3v-7L8 1.5z"/><path d="M8 1.5v13M14 4.5L8 8 2 4.5"/></svg>`,
    rocket: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M3 13c0-2.5 1.5-4.5 3-5 0-3 3-6 7-6 0 4-3 7-6 7-.5 1.5-2.5 3-5 3l1-2.5-2.5-1L3 13z"/><circle cx="9.5" cy="5.5" r="1"/></svg>`,
    target: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="3"/><circle cx="8" cy="8" r="0.8" fill="currentColor"/></svg>`,
    key: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="8" r="3.5"/><path d="M8.5 8H14v2.5M11.5 8v2"/></svg>`,
    brain: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3.5a2.5 2.5 0 00-4.5 1.5c0 .6.2 1.1.6 1.5a2.5 2.5 0 000 3c-.4.4-.6.9-.6 1.5a2.5 2.5 0 004.5 1.5M10 3.5a2.5 2.5 0 014.5 1.5c0 .6-.2 1.1-.6 1.5a2.5 2.5 0 010 3c.4.4.6.9.6 1.5a2.5 2.5 0 01-4.5 1.5M6 3.5V13M10 3.5V13"/></svg>`,
    link: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 9.5a3 3 0 004.2 0l2.3-2.3a3 3 0 00-4.2-4.2l-1.2 1.2M9.5 6.5a3 3 0 00-4.2 0L3 8.8a3 3 0 004.2 4.2l1.2-1.2"/></svg>`,
    clock: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><polyline points="8 4 8 8 10.5 9.5"/></svg>`,
    market: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3.5h12l-1.5 4h-9L2 3.5z"/><rect x="3.5" y="7.5" width="9" height="6.5" rx="0.5"/><path d="M6.5 10.5h3"/></svg>`,
    alert: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.8l6.8 11.7a1 1 0 01-.87 1.5H2.07a1 1 0 01-.87-1.5L8 1.8z"/><line x1="8" y1="6.5" x2="8" y2="9.5"/><circle cx="8" cy="12" r="0.8" fill="currentColor"/></svg>`,
    lock: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6.5" width="10" height="7.5" rx="1.5"/><path d="M5.5 6.5V4.5a2.5 2.5 0 015 0v2"/></svg>`,
    statusDot: (col = 'green') => `<span class="bx-status-dot ${col}"></span>`,
    layers: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><polygon points="8 1.5 14.5 5 8 8.5 1.5 5"/><polyline points="1.5 8 8 11.5 14.5 8"/><polyline points="1.5 11 8 14.5 14.5 11"/></svg>`,
    tag: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8.5L8.5 13.5a1.4 1.4 0 01-2 0L2 9V2.5h6.5l5 5a1.4 1.4 0 010 2z"/><circle cx="5" cy="5.5" r="1" fill="currentColor"/></svg>`,
    box: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5l5.5 3.2v6.6L8 14.5l-5.5-3.2V4.7L8 1.5z"/><polyline points="2.5 4.7 8 8 13.5 4.7"/><line x1="8" y1="8" x2="8" y2="14.5"/></svg>`,
    globe: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><ellipse cx="8" cy="8" rx="3" ry="6.5"/><line x1="1.5" y1="8" x2="14.5" y2="8"/></svg>`,
    alignLeft: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><line x1="2.5" y1="3.5" x2="13.5" y2="3.5"/><line x1="2.5" y1="6.5" x2="9.5" y2="6.5"/><line x1="2.5" y1="9.5" x2="12" y2="9.5"/><line x1="2.5" y1="12.5" x2="7.5" y2="12.5"/></svg>`,
    shieldCheck: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5l5 2v4.5c0 3.5-2.5 6.5-5 7.5-2.5-1-5-4-5-7.5V3.5l5-2z"/><polyline points="5.5 8 7 9.5 10.5 6"/></svg>`,
    pieChart: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 8A6.5 6.5 0 118 1.5v6.5h6.5z"/><path d="M9.5 1.7A6.5 6.5 0 0114.3 6.5H9.5V1.7z"/></svg>`,
    user: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="5" r="2.8"/><path d="M2.5 13.5c0-2.8 2.5-4.5 5.5-4.5s5.5 1.7 5.5 4.5"/></svg>`,
    clock: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><polyline points="8 4.5 8 8 10.5 9.5"/></svg>`,
    alertTriangle: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.2l6 10.5H2L8 2.2zM8 6.5v3M8 11.5v.5"/></svg>`,
    gauge: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><path d="M8 8l2.5-2.5"/><path d="M3.5 11.5a5 5 0 019 0"/></svg>`,
    cpu: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="8" height="8" rx="1.5"/><line x1="6" y1="1.5" x2="6" y2="4"/><line x1="10" y1="1.5" x2="10" y2="4"/><line x1="6" y1="12" x2="6" y2="14.5"/><line x1="10" y1="12" x2="10" y2="14.5"/><line x1="1.5" y1="6" x2="4" y2="6"/><line x1="1.5" y1="10" x2="4" y2="10"/><line x1="12" y1="6" x2="14.5" y2="6"/><line x1="12" y1="10" x2="14.5" y2="10"/></svg>`,
    gitBranch: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><line x1="4.5" y1="3" x2="4.5" y2="13"/><circle cx="4.5" cy="3" r="1.5"/><circle cx="4.5" cy="13" r="1.5"/><circle cx="11.5" cy="5.5" r="1.5"/><path d="M4.5 9.5a4 4 0 004-4h3"/></svg>`,
    target: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.5"/><circle cx="8" cy="8" r="3"/><circle cx="8" cy="8" r="0.5" fill="currentColor"/></svg>`,
    shuffle: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="10.5 3 14 3 14 6.5"/><path d="M2.5 13.5l4-4 2 2 4.5-7.5"/><polyline points="14 10 14 13.5 10.5 13.5"/><path d="M2.5 3l4.5 4.5-1.5 1.5"/></svg>`,
    key: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="5.5" cy="6.5" r="3"/><path d="M7.7 8.7L14 15M11.5 12.5l1.8 1.8M13.5 10.5l1.5 1.5"/></svg>`,
    cube: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L14 5v6l-6 3.5L2 11V5L8 1.5z"/><polyline points="2 5 8 8.5 14 5"/><line x1="8" y1="8.5" x2="8" y2="14.5"/></svg>`,
    flame: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5c.8 2 3.5 4.2 3.5 7a5 5 0 01-10 0c0-3.5 3-5.5 4-7.5.5 2.5 2 3.5 2.5 3.5"/></svg>`,
    wrench: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4.5a3.5 3.5 0 01-4.8 3.3L4.5 12.5a1.4 1.4 0 01-2-2l4.7-4.7A3.5 3.5 0 0110.5 1l-2 2 1.5 1.5 4-2z"/></svg>`,
    chevronDown: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6l4 4 4-4"/></svg>`,
    shieldCheck: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5l5 2v4.5c0 3.5-2.5 6.2-5 7-2.5-.8-5-3.5-5-7V3.5l5-2z"/><path d="M6 8l1.5 1.5L10.5 6"/></svg>`,
    save: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12.5 14H3.5a1 1 0 01-1-1V3a1 1 0 011-1h7l3 3v8a1 1 0 01-1 1z"/><path d="M10.5 14v-4.5h-5V14"/><path d="M5.5 2v3h4V2"/></svg>`,
    sliders: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><line x1="2.5" y1="4" x2="13.5" y2="4"/><line x1="2.5" y1="8" x2="13.5" y2="8"/><line x1="2.5" y1="12" x2="13.5" y2="12"/><circle cx="5" cy="4" r="1.5" fill="currentColor"/><circle cx="11" cy="8" r="1.5" fill="currentColor"/><circle cx="7" cy="12" r="1.5" fill="currentColor"/></svg>`,
    zap: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><polygon points="8.5 1.5 2.5 9 7.5 9 7.5 14.5 13.5 7 8.5 7 8.5 1.5"/></svg>`,
    bot: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="10" height="8" rx="2"/><circle cx="6" cy="8.5" r="1" fill="currentColor"/><circle cx="10" cy="8.5" r="1" fill="currentColor"/><path d="M8 2v2.5M1.5 8.5h1.5M13 8.5h1.5"/></svg>`,
    brain: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3a2.5 2.5 0 00-4 2 2.5 2.5 0 001 2.3 2.5 2.5 0 000 4.4A2.5 2.5 0 006 13.5V11a2 2 0 012-2 2 2 0 012 2v2.5a2.5 2.5 0 003-1.8 2.5 2.5 0 000-4.4 2.5 2.5 0 001-2.3 2.5 2.5 0 00-4-2h-.5A2.5 2.5 0 008 4a2.5 2.5 0 00-1.5-1H6z"/></svg>`,
    thermometer: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 7.5V3a1.5 1.5 0 00-3 0v4.5a3 3 0 103 0z"/><circle cx="8" cy="11.5" r="1.2" fill="currentColor"/></svg>`,
    sparkles: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 1.5L9.5 6 14 7.5 9.5 9 8 13.5 6.5 9 2 7.5 6.5 6 8 1.5z"/><path d="M12.5 1.5l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9-1.9-.6 1.9-.6.6-1.9z"/></svg>`,
    lock: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="6.5" width="9" height="7" rx="1.5"/><path d="M5.5 6.5V4.5a2.5 2.5 0 015 0v2"/></svg>`,
    chat: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 3.5a1.5 1.5 0 011.5-1.5h8a1.5 1.5 0 011.5 1.5v6a1.5 1.5 0 01-1.5 1.5H6l-3.5 2.5V3.5z"/></svg>`,
    refresh: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 7A5.5 5.5 0 108 13.5c2.3 0 4.2-1.4 5-3.3"/><path d="M13.5 3v4h-4"/></svg>`,
    stop: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="9" height="9" rx="1.5"/></svg>`,
    calendar: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3.5" width="12" height="10" rx="1.5"/><line x1="2" y1="6.5" x2="14" y2="6.5"/><line x1="5" y1="2" x2="5" y2="4.5"/><line x1="11" y1="2" x2="11" y2="4.5"/></svg>`,
    close: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>`,
    alert: (s = 14, cls = '') => `<svg class="bx-icon ${cls}" width="${s}" height="${s}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.5L1.5 13.5h13L8 2.5z"/><path d="M8 6.5v3M8 11.5v.5"/></svg>`,
  };

  const LANG_COLORS = {
    python: '#3572A5',
    javascript: '#f1e05a',
    typescript: '#3178c6',
    html: '#e34c26',
    css: '#563d7c',
    go: '#00ADD8',
    rust: '#dea584',
    java: '#b07219',
    c: '#555555',
    'c++': '#f34b7d',
    'c#': '#178600',
    ruby: '#701516',
    php: '#4F5D95',
    swift: '#F05138',
    kotlin: '#A97BFF',
    shell: '#89e051',
    bash: '#89e051',
    sh: '#89e051',
    json: '#292929',
    yaml: '#cb171e',
    markdown: '#083fa1',
    vue: '#41b883',
    sql: '#e38c00',
  };

  function showToast(msg, isError = false) {
    let t = document.getElementById('bx-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'bx-toast';
      document.body.appendChild(t);
    }
    t.innerText = msg;
    t.style.background = isError ? '#ef4444' : '#10b981';
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2800);
  }

  function createModalDOM() {
    if (document.getElementById('bx-settings-overlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'bx-settings-overlay';
    overlay.style.display = 'none';
    overlay.innerHTML = `
      <div id="bx-settings-modal" role="dialog" aria-modal="true">
        <!-- Header -->
        <div class="bx-modal-header">
          <div class="bx-modal-title-wrap">
            <span class="bx-modal-icon">${BxIcons.settings(22)}</span>
            <div>
              <h3 class="bx-modal-title">项目全景配置与五大门禁治理</h3>
              <div style="font-size: 0.75rem; color: var(--bx-text-muted); margin-top: 2px;" id="bx-proj-path">正在获取工作区路径...</div>
            </div>
          </div>
          <div class="bx-header-actions">
            <label class="bx-header-archive-label" title="显示已归档项目">
              <input type="checkbox" id="bx-show-archived-cb" />
              <span>含归档</span>
            </label>
            <select id="bx-proj-selector" class="bx-proj-select" title="选择受管工作区项目"></select>
            <select id="bx-ide-selector" class="bx-proj-select" style="font-size: 0.78rem;" title="选择本地已安装的 IDE 或 AI 工具">
              <option value="vscode">VS Code</option>
              <option value="cursor">Cursor</option>
            </select>
            <button type="button" class="bx-action-btn primary" id="bx-open-ide-btn" title="在所选 IDE 或 AI 工具中打开当前工作区 (REQ-04)">
              <span>${BxIcons.launch(14)}</span><span>打开项目</span>
            </button>
            <button type="button" class="bx-action-btn" id="bx-archive-proj-btn" title="归档或取消归档当前项目 (REQ-06)">
              <span>${BxIcons.archive(14)}</span><span id="bx-archive-btn-text">归档</span>
            </button>
            <button type="button" class="bx-action-btn danger" id="bx-delete-proj-btn" title="从列表移除或彻底清除 .bugx 数据 (REQ-06)">
              <span>${BxIcons.trash(14)}</span><span>删除</span>
            </button>
            <button class="bx-modal-close" id="bx-modal-close-btn" title="关闭 (Esc)">&times;</button>
          </div>
        </div>

        <!-- Tabs -->
        <div class="bx-modal-tabs">
          <button class="bx-tab-btn active" data-tab="gates">${BxIcons.shield(14)} <span>门禁与模型参数</span></button>
          <button class="bx-tab-btn" data-tab="mining">${BxIcons.bolt(14)} <span>挖掘监控与覆盖矩阵</span></button>
          <button class="bx-tab-btn" data-tab="profile">${BxIcons.compass(14)} <span>项目信息与定位</span></button>
          <button class="bx-tab-btn" data-tab="mcps">${BxIcons.plug(14)} <span>MCP 管理与市场</span> <span class="bx-tab-count" id="bx-mcps-count">0</span></button>
          <button class="bx-tab-btn" data-tab="skills">${BxIcons.bolt(14)} <span>Skill 管理与市场</span> <span class="bx-tab-count" id="bx-skills-count">0</span></button>
          <button class="bx-tab-btn" data-tab="docs">${BxIcons.book(14)} <span>项目文档中心</span></button>
          <button class="bx-tab-btn" data-tab="files">${BxIcons.files(14)} <span>工作区文件管理</span></button>
        </div>

        <!-- Body -->
        <div class="bx-modal-body">
          <!-- 1. Five Gates Tab -->
          <div class="bx-tab-content active" id="bx-tab-gates">
            <div class="bx-card">
              <div class="bx-card-title">
                <span>五大启动与自愈安全门禁 (DOC-167 架构标准)</span>
                <span class="bx-badge bx-badge-green" id="bx-gate-overall-badge"><span class="bx-status-dot active"></span> 门禁策略运行中</span>
              </div>

              <!-- Gate 1: Cloud Sync -->
              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span>1. 云端同步门禁 (Cloud Sync Gate)</span>
                    <span class="bx-badge" id="bx-cloudsync-badge"><span class="bx-status-dot inactive"></span> 未绑定</span>
                  </div>
                  <div class="bx-gate-desc">
                    是否将脱敏经验、错题条目与治理画像同步至 BugX 企业级私有云中枢，共享跨项目漏洞免疫规则。
                  </div>
                </div>
                <div class="bx-gate-ctrls">
                  <div class="bx-gate-select-wrap">
                    <select id="bx-select-cloud-policy" class="bx-proj-select bx-gate-select">
                      <option value="authorized">授权同步 (authorized)</option>
                      <option value="local">纯本地自治 (local)</option>
                      <option value="full">全量同步 (full)</option>
                      <option value="block">禁止出网 (block)</option>
                    </select>
                  </div>
                  <div class="bx-gate-switch-wrap">
                    <label class="bx-switch">
                      <input type="checkbox" id="bx-toggle-cloudsync">
                      <span class="bx-slider"></span>
                    </label>
                  </div>
                </div>
              </div>

              <!-- Gate 2: Review Mode -->
              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span>2. 审查方式门禁 (Review Mode Gate)</span>
                    <span class="bx-badge bx-badge-cyan" id="bx-review-badge"><span class="bx-status-dot active"></span> 全自动</span>
                  </div>
                  <div class="bx-gate-desc">
                    漏洞勘测与代码审查的交互深度。全自动模式由模型自主深入，交互模式逐步呈现方案供用户确认。
                  </div>
                </div>
                <div class="bx-gate-ctrls">
                  <div class="bx-gate-select-wrap">
                    <select id="bx-select-review" class="bx-proj-select bx-gate-select">
                      <option value="auto">全自动深入审查 (auto)</option>
                      <option value="interactive">交互式逐步确认 (interactive)</option>
                      <option value="off">关闭审查 (off)</option>
                    </select>
                  </div>
                  <div class="bx-gate-switch-wrap">
                    <span class="bx-switch-placeholder"></span>
                  </div>
                </div>
              </div>

              <!-- Gate 3: Polling Policy -->
              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span>3. 轮询策略门禁 (Polling Policy Gate)</span>
                    <span class="bx-badge bx-badge-cyan" id="bx-polling-badge"><span class="bx-status-dot active"></span> 持续轮询</span>
                  </div>
                  <div class="bx-gate-desc">
                    持续漏洞挖掘与长程自愈机制。持续轮询将自动开展多轮覆盖勘测，手动触发仅执行单次。
                  </div>
                </div>
                <div class="bx-gate-ctrls">
                  <div class="bx-gate-select-wrap">
                    <select id="bx-select-polling" class="bx-proj-select bx-gate-select">
                      <option value="auto">持续自治轮询 (auto)</option>
                      <option value="manual">单轮手动触发 (manual)</option>
                      <option value="off">关闭轮询 (off)</option>
                    </select>
                  </div>
                  <div class="bx-gate-switch-wrap">
                    <span class="bx-switch-placeholder"></span>
                  </div>
                </div>
              </div>

              <!-- Gate 4: Fix Mode (Auto Fix Code) -->
              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span>4. 修复模式门禁 (Fix Mode / 自动修改代码)</span>
                    <span class="bx-badge" id="bx-autofix-badge"><span class="bx-status-dot active"></span> 自动合入</span>
                  </div>
                  <div class="bx-gate-desc">
                    发现漏洞时，是否允许自动合成防御性源码补丁并直接回填修改代码。关闭后仅产出审计报告。
                  </div>
                </div>
                <div class="bx-gate-ctrls">
                  <div class="bx-gate-select-wrap">
                    <select id="bx-select-fix-mode" class="bx-proj-select bx-gate-select">
                      <option value="auto">自动修改并合入补丁 (auto)</option>
                      <option value="confirm">生成补丁待用户批准 (confirm)</option>
                      <option value="off">仅报告绝不修改源码 (off)</option>
                    </select>
                  </div>
                  <div class="bx-gate-switch-wrap">
                    <label class="bx-switch">
                      <input type="checkbox" id="bx-toggle-autofix">
                      <span class="bx-slider"></span>
                    </label>
                  </div>
                </div>
              </div>

              <!-- Gate 5: Commit Policy -->
              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span>5. 提交策略门禁 (Git Commit Policy Gate)</span>
                    <span class="bx-badge bx-badge-gray" id="bx-commit-badge"><span class="bx-status-dot active"></span> 轮次提交</span>
                  </div>
                  <div class="bx-gate-desc">
                    漏洞修复与验证成功后的 Git 提交频率。可按每轮审计自动提交，或按单一 Task 提交。
                  </div>
                </div>
                <div class="bx-gate-ctrls">
                  <div class="bx-gate-select-wrap">
                    <select id="bx-select-commit" class="bx-proj-select bx-gate-select">
                      <option value="round">每轮自动提交 (round)</option>
                      <option value="task">按任务提交 (task)</option>
                      <option value="off">关闭自动提交 (off)</option>
                    </select>
                  </div>
                  <div class="bx-gate-switch-wrap">
                    <span class="bx-switch-placeholder"></span>
                  </div>
                </div>
              </div>

              <!-- Auxiliary Protection -->
              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span style="display: inline-flex; align-items: center; gap: 6px; color: var(--bx-brand-sky-deep);">${BxIcons.shieldCheck(16)} 带外物理沙箱备份守护 (Sandbox Physical Backup)</span>
                    <span class="bx-badge bx-badge-cyan">强制开启</span>
                  </div>
                  <div class="bx-gate-desc">
                    任何补丁合入与代码变更前，强制在受控物理沙箱创建瞬态全量镜像备份，确保毫秒级零损回滚。
                  </div>
                </div>
              </div>

              <!-- Explicit Save Button Bar -->
              <div class="bx-save-bar">
                <span class="bx-save-status" id="bx-save-status-text">${BxIcons.check(13)} 当前配置已与磁盘保持一致</span>
                <button type="button" class="bx-save-btn" id="bx-save-all-gates-btn">
                  <span>${BxIcons.save(14)}</span>
                  <span>保存门禁与模型配置</span>
                </button>
              </div>
            </div>

            <!-- Card 2: Model & Agent Runtime Parameter Tuning (REQ-01 / REQ-03) -->
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.sliders(16)} 多宿主 Agent 调度与模型生成参数 (Model &amp; Runtime Parameters)</span>
                <span class="bx-badge bx-badge-cyan" style="display: inline-flex; align-items: center; gap: 4px;">${BxIcons.zap(12)} 下个任务热生效 (Next-Task Hot Reload)</span>
              </div>

              <div class="bx-model-config-grid">
                <!-- Default Host -->
                <div class="bx-param-box">
                  <div class="bx-param-label-row">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.bot(14)} 默认执行宿主 (Default Host)</span>
                  </div>
                  <select id="bx-mc-default-host" class="bx-proj-select" style="width: 100%; font-size: 0.82rem;">
                    <option value="cline">Cline CLI (cline -y --json)</option>
                    <option value="claude">Claude Code (claude -p)</option>
                    <option value="codex">OpenAI Codex (codex exec)</option>
                    <option value="opencode">OpenCode (opencode run)</option>
                    <option value="pi">Pi Coding Agent (pi -p)</option>
                    <option value="zcode">ZCode Agent (zcode)</option>
                    <option value="dsh">DSH / Raw Shell (dsh)</option>
                    <option value="bugx">BugX Autonomous Engine (bugx)</option>
                  </select>
                </div>

                <!-- Default Model -->
                <div class="bx-param-box">
                  <div class="bx-param-label-row">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.brain(14)} 默认主模型标识 (Default Model)</span>
                  </div>
                  <input type="text" id="bx-mc-default-model" class="bx-proj-select" style="width: 100%; font-size: 0.82rem;" placeholder="例如 claude-sonnet-4-6 / gpt-5.4 / auto" value="auto" />
                </div>

                <!-- Temperature Slider -->
                <div class="bx-param-box">
                  <div class="bx-param-label-row">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.thermometer(14)} 模型采样温度 (Temperature: 0.0 ~ 2.0)</span>
                    <span class="bx-param-val" id="bx-mc-temp-val">0.20</span>
                  </div>
                  <input type="range" id="bx-mc-temperature" class="bx-range-slider" min="0" max="2" step="0.05" value="0.2" />
                  <div style="font-size: 0.72rem; color: var(--bx-text-muted); margin-top: 4px;">低温度 (0.1~0.3) 适合确定性代码修复；高温度 (0.6~1.0) 适合发散式漏洞假设生成。</div>
                </div>

                <!-- Top-P Slider -->
                <div class="bx-param-box">
                  <div class="bx-param-label-row">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.target(14)} 核采样阈值 (Top-P: 0.0 ~ 1.0)</span>
                    <span class="bx-param-val" id="bx-mc-topp-val">0.95</span>
                  </div>
                  <input type="range" id="bx-mc-top-p" class="bx-range-slider" min="0" max="1" step="0.05" value="0.95" />
                  <div style="font-size: 0.72rem; color: var(--bx-text-muted); margin-top: 4px;">控制候选 Token 累积概率分布，推荐保持在 0.90 ~ 0.95。</div>
                </div>

                <!-- Reasoning Effort -->
                <div class="bx-param-box">
                  <div class="bx-param-label-row">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.sparkles(14)} 深度思考推理强度 (Reasoning Effort)</span>
                  </div>
                  <select id="bx-mc-reasoning" class="bx-proj-select" style="width: 100%; font-size: 0.82rem;">
                    <option value="low">Low (快速响应)</option>
                    <option value="medium">Medium (均衡分析)</option>
                    <option value="high">High (深度漏洞推演)</option>
                    <option value="xhigh">XHigh (极限对抗审计)</option>
                  </select>
                </div>

                <!-- Worker Concurrency & Max Tokens -->
                <div class="bx-param-box">
                  <div class="bx-param-label-row">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.cpu(14)} 挖掘并发 Worker 数 (1~8) &amp; Max Tokens</span>
                    <span class="bx-param-val" id="bx-mc-conc-val">4 Workers</span>
                  </div>
                  <div style="display: flex; align-items: center; gap: 10px;">
                    <input type="range" id="bx-mc-concurrency" class="bx-range-slider" min="1" max="8" step="1" value="4" style="flex: 1;" />
                    <input type="number" id="bx-mc-max-tokens" class="bx-proj-select" style="width: 110px; font-size: 0.78rem;" min="256" max="200000" step="1024" value="16384" title="单次最大生成 Token 数 (256~200000)" />
                  </div>
                </div>
              </div>

              <!-- High Risk Write Approval & Auto-Mount Switches -->
              <div class="bx-gate-row" style="margin-top: 12px;">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.lock(14)} 高危写文件与破坏性命令二次确认 (High-Risk Write Approval)</span>
                  </div>
                  <div class="bx-gate-desc">开启后，除沙箱自测外，涉及覆盖核心源码或高危命令时将在 Web 端弹出权限确认卡片 (REQ-01 L-1)。</div>
                </div>
                <label class="bx-switch">
                  <input type="checkbox" id="bx-mc-high-risk-approval">
                  <span class="bx-slider"></span>
                </label>
              </div>

              <div class="bx-gate-row">
                <div class="bx-gate-info">
                  <div class="bx-gate-name">
                    <span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.sparkles(14)} 首次启动自动挂载无凭据推荐 MCP / Skill (Auto-Mount Capabilities)</span>
                  </div>
                  <div class="bx-gate-desc">根据项目语言与暴露面画像，自动挂载无需外部 API Key 的高匹配度 MCP 与 Skill (REQ-09 L-8)。</div>
                </div>
                <label class="bx-switch">
                  <input type="checkbox" id="bx-mc-auto-mount" checked>
                  <span class="bx-slider"></span>
                </label>
              </div>
            </div>

            <!-- 3. Project Conversation History Management Card (REQ-02) -->
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.chat(15)} 项目专属对话与挖掘会话管理 (REQ-02: 查看 / 跨宿主恢复 / 重命名 / 删除)</span>
                <button type="button" class="bx-action-btn" id="bx-refresh-sessions-btn"><span>${BxIcons.refresh(13)}</span> <span>刷新会话列表</span></button>
              </div>
              <table class="bx-table" style="font-size: 0.8rem;">
                <thead>
                  <tr>
                    <th>会话标题与 ID</th>
                    <th>执行宿主</th>
                    <th>状态</th>
                    <th>更新时间</th>
                    <th>操作 (恢复 / 重命名 / 删除)</th>
                  </tr>
                </thead>
                <tbody id="bx-sessions-table-body">
                  <tr><td colspan="5" style="text-align: center; color: var(--bx-text-muted);">正在加载项目历史对话会话...</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- 2. Mining Intelligence, Strategy & Live Dynamic Monitoring Tab -->
          <div class="bx-tab-content" id="bx-tab-mining">
            <!-- 0. Live Monitoring Control Bar -->
            <div class="bx-live-bar">
              <div class="bx-live-left">
                <span class="bx-live-pulse-dot"></span>
                <span class="bx-live-label" id="bx-live-monitor-text">LIVE 动态监控在线</span>
                <!-- ECG SVG animation -->
                <svg class="bx-ecg-svg" viewBox="0 0 60 20">
                  <path class="bx-ecg-path" d="M0,10 L15,10 L20,3 L25,17 L30,6 L35,14 L40,10 L60,10" />
                </svg>
                <span id="bx-live-round-info" style="font-size: 0.78rem; color: var(--bx-text-muted);">正在侦听挖掘信号...</span>
              </div>
              <div class="bx-live-right">
                <button type="button" class="bx-action-btn danger" id="bx-cancel-mining-btn" title="手动取消正在执行的任务并保留已完成策略组断点进度 (REQ-08)">
                  <span>${BxIcons.stop(13)}</span>
                  <span>手动取消任务 (保留进度)</span>
                </button>
                <select id="bx-live-interval-select" class="bx-proj-select" style="font-size: 0.78rem;">
                  <option value="3">动态轮询 (3秒刷新)</option>
                  <option value="8">平稳轮询 (8秒刷新)</option>
                  <option value="0">暂停自动刷新</option>
                </select>
                <button type="button" class="bx-live-btn" id="bx-live-refresh-btn" title="立即获取最新挖掘状态">
                  <span class="bx-spin-icon">${BxIcons.refresh(13)}</span>
                  <span>立即刷新</span>
                </button>
              </div>
            </div>

            <!-- 1. Flowing Execution Pipeline Card -->
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.zap(15)} 全息流光执行管道与策略分组覆盖矩阵 (Pipeline &amp; Strategy Waffle Grid)</span>
                <span class="bx-badge" id="bx-mining-status-badge"><span class="bx-status-dot inactive"></span> 获取进度中</span>
              </div>
              
              <!-- Flowing Pipeline Topology -->
              <div class="bx-flowing-pipeline" id="bx-flowing-pipeline-box">
                <!-- Dynamically generated with glowing particles and active radar ripple -->
              </div>

              <!-- Radial Gauge & Progress Summary -->
              <div class="bx-radial-wrap">
                <!-- SVG Radial Gauge -->
                <svg class="bx-gauge-svg" viewBox="0 0 64 64">
                  <defs>
                    <linearGradient id="bx-gauge-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                      <stop offset="0%" stop-color="#06b6d4" />
                      <stop offset="100%" stop-color="#10b981" />
                    </linearGradient>
                  </defs>
                  <circle class="bx-gauge-bg" cx="32" cy="32" r="26" />
                  <circle class="bx-gauge-val" id="bx-gauge-val-circle" cx="32" cy="32" r="26" stroke-dasharray="163.36" stroke-dashoffset="163.36" />
                </svg>
                <div style="flex: 1;">
                  <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px;">
                    <span style="font-size: 0.82rem; font-weight: 600; color: var(--bx-text-main);">目标探测单元与策略任务组覆盖进度</span>
                    <span id="bx-coverage-detail" style="font-size: 0.95rem; font-weight: 700; color: var(--bx-accent-green);">0 / 0 (0.0%)</span>
                  </div>
                  <div class="bx-progress-bar-bg" style="margin-bottom: 8px;">
                    <div class="bx-progress-bar-fill" id="bx-coverage-bar" style="width: 0%;"></div>
                  </div>
                  <div style="display: flex; flex-wrap: wrap; gap: 14px; font-size: 0.78rem; color: var(--bx-text-muted);">
                    <div style="display: inline-flex; align-items: center; gap: 4px;">${BxIcons.clock(12)} 累计耗时: <strong id="bx-mining-elapsed" style="color: var(--bx-text-main);">0s</strong></div>
                    <div style="display: inline-flex; align-items: center; gap: 4px;">${BxIcons.wrench(12)} 工具调用: <strong id="bx-mining-tools" style="color: var(--bx-text-main);">0 次</strong></div>
                    <div style="display: inline-flex; align-items: center; gap: 4px;">${BxIcons.cpu(12)} Token 消耗: <strong id="bx-mining-tokens" style="color: var(--bx-text-main);">0</strong></div>
                    <div style="display: inline-flex; align-items: center; gap: 4px;">${BxIcons.target(12)} 当前阶段: <strong id="bx-mining-current-phase" style="color: var(--bx-accent-cyan);">就绪</strong></div>
                  </div>
                </div>
              </div>

              <!-- REQ-07 Strategy-Group Waffle Coverage Matrix -->
              <div class="bx-waffle-header">
                <div style="font-size: 0.82rem; font-weight: 600; color: var(--bx-text-main); display: inline-flex; align-items: center; gap: 6px;">
                  <span>${BxIcons.layers(14)} 挖掘策略分组小方块覆盖矩阵 (Waffle Coverage Grid)</span>
                  <span class="bx-badge bx-badge-cyan" id="bx-waffle-count-badge" style="margin-left: 6px;">0 / 0 策略组</span>
                </div>
                <div class="bx-waffle-legend">
                  <span class="bx-waffle-legend-item"><i class="bx-waffle-swatch" style="background:#334155;"></i>待挖掘 (Pending)</span>
                  <span class="bx-waffle-legend-item"><i class="bx-waffle-swatch" style="background:#0284c7;"></i>探测中 (Running)</span>
                  <span class="bx-waffle-legend-item"><i class="bx-waffle-swatch" style="background:#10b981;"></i>已覆盖安全 (Clean)</span>
                  <span class="bx-waffle-legend-item"><i class="bx-waffle-swatch" style="background:#f59e0b;"></i>存在疑点 (Suspect)</span>
                  <span class="bx-waffle-legend-item"><i class="bx-waffle-swatch" style="background:#ef4444;"></i>确认漏洞 (Vulnerable)</span>
                </div>
              </div>
              <div class="bx-waffle-grid" id="bx-waffle-grid"></div>
              <div class="bx-waffle-drawer" id="bx-waffle-drawer"></div>
            </div>

            <!-- 2. Dual Dynamic Charts Grid: Round Trend & Attack Surface Radar -->
            <div class="bx-charts-grid">
              <!-- Chart 1: Round Trend Area Chart -->
              <div class="bx-chart-box">
                <div class="bx-chart-title-bar">
                  <div class="bx-chart-title">
                    <span class="bx-chart-title-icon" style="color: #0284c7;">${BxIcons.trendingUp(15)}</span>
                    <span>多轮次挖掘动态趋势 (Round Trend Chart)</span>
                  </div>
                  <div class="bx-chart-legend">
                    <div class="bx-legend-item">
                      <span class="bx-legend-dot" style="background: #06b6d4;"></span>
                      <span>耗时 (秒)</span>
                    </div>
                    <div class="bx-legend-item">
                      <span class="bx-legend-dot" style="background: #f97316;"></span>
                      <span>发现漏洞 (个)</span>
                    </div>
                  </div>
                </div>
                <div class="bx-trend-chart-wrap" id="bx-trend-chart-wrap">
                  <!-- SVG Dynamic Trend Chart -->
                  <svg class="bx-trend-svg" id="bx-trend-svg" viewBox="0 0 460 180" preserveAspectRatio="none">
                    <defs>
                      <linearGradient id="bx-cyan-grad" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stop-color="#06b6d4" stop-opacity="0.38" />
                        <stop offset="100%" stop-color="#06b6d4" stop-opacity="0.02" />
                      </linearGradient>
                    </defs>
                    <g id="bx-trend-grid"></g>
                    <path id="bx-trend-area" class="bx-area-fill" d="" fill="url(#bx-cyan-grad)" />
                    <path id="bx-trend-line-elapsed" class="bx-chart-line" d="" stroke="#06b6d4" />
                    <path id="bx-trend-line-findings" class="bx-chart-line" d="" stroke="#f97316" />
                    <g id="bx-trend-dots"></g>
                    <line id="bx-trend-crosshair" class="bx-chart-crosshair" x1="0" y1="0" x2="0" y2="180" style="display:none;" />
                  </svg>
                  <div class="bx-chart-tooltip" id="bx-trend-tooltip"></div>
                </div>
              </div>

              <!-- Chart 2: Dynamic Radar Chart -->
              <div class="bx-chart-box">
                <div class="bx-chart-title-bar">
                  <div class="bx-chart-title">
                    <span class="bx-chart-title-icon" style="color: #10b981;">${BxIcons.radar(16)}</span>
                    <span>多维安全暴露面雷达 (Attack Radar)</span>
                  </div>
                  <span class="bx-badge bx-badge-green" id="bx-radar-health-badge">Grade B</span>
                </div>
                <div class="bx-radar-chart-wrap" id="bx-radar-chart-wrap">
                  <!-- Dynamic High-Precision SVG Radar Chart -->
                  <svg class="bx-radar-svg" id="bx-radar-svg" viewBox="0 0 340 220">
                    <defs>
                      <linearGradient id="bx-radar-poly-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                        <stop offset="0%" stop-color="#10b981" stop-opacity="0.36" />
                        <stop offset="50%" stop-color="#06b6d4" stop-opacity="0.25" />
                        <stop offset="100%" stop-color="#3b82f6" stop-opacity="0.14" />
                      </linearGradient>
                      <filter id="bx-radar-glow" x="-20%" y="-20%" width="140%" height="140%">
                        <feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#10b981" flood-opacity="0.2" />
                      </filter>
                    </defs>
                    <g id="bx-radar-grid-layer"></g>
                    <polygon id="bx-radar-polygon" class="bx-radar-poly" points="" filter="url(#bx-radar-glow)" />
                    <g id="bx-radar-points-layer"></g>
                    <g id="bx-radar-labels-layer"></g>
                  </svg>
                  <div class="bx-chart-tooltip" id="bx-radar-tooltip"></div>
                </div>
                <div class="bx-radar-metrics-bar" id="bx-radar-metrics-bar"></div>
              </div>
            </div>

            <!-- 2. Mining Strategy Card -->
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.shield(15)} 挖掘攻防策略与剖面矩阵 (Strategy &amp; Profiles)</span>
                <span class="bx-badge bx-badge-cyan" id="bx-strategy-grade-badge">安全评级: B</span>
              </div>

              <!-- Strategy Core Info -->
              <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 12px;">
                <div class="bx-mini-card" style="padding: 8px 12px; font-size: 0.82rem;">
                  <div style="color: var(--bx-text-muted); font-size: 0.74rem;">轮询策略模式</div>
                  <strong id="bx-strat-polling" style="color: var(--bx-accent-green);">持续自治 (auto)</strong>
                </div>
                <div class="bx-mini-card" style="padding: 8px 12px; font-size: 0.82rem;">
                  <div style="color: var(--bx-text-muted); font-size: 0.74rem;">审查确认深度</div>
                  <strong id="bx-strat-review" style="color: var(--bx-accent-cyan);">全自动深入 (auto)</strong>
                </div>
                <div class="bx-mini-card" style="padding: 8px 12px; font-size: 0.82rem;">
                  <div style="color: var(--bx-text-muted); font-size: 0.74rem;">调度与并发基线</div>
                  <span id="bx-strat-scale" style="color: var(--bx-text-main); font-weight: 500;">推荐 4 并发 / 5 轮次</span>
                </div>
              </div>

              <!-- Surfaces & Profiles -->
              <div style="font-size: 0.8rem; margin-bottom: 6px; color: var(--bx-text-muted);">
                <span>覆盖语言与技术栈:</span>
                <div class="bx-tag-group" id="bx-strat-languages"></div>
              </div>
              <div style="font-size: 0.8rem; margin-bottom: 6px; color: var(--bx-text-muted);">
                <span>探测攻击暴露面 (Attack Surfaces):</span>
                <div class="bx-tag-group" id="bx-strat-surfaces"></div>
              </div>
              <div style="font-size: 0.8rem; color: var(--bx-text-muted);">
                <span>激活的安全检测剖面 (Check Profiles):</span>
                <div class="bx-tag-group" id="bx-strat-profiles"></div>
              </div>

              <!-- Architecture Mode Flow Diagram Section -->
              <div class="bx-arch-flow-section">
                <div class="bx-arch-flow-header">
                  <div class="bx-arch-flow-title-group">
                    <span class="bx-arch-flow-title" id="bx-arch-title-text" style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.code(14)} 漏洞深度挖掘流转图 (Bug Mining Flow)</span>
                    <span class="bx-arch-flow-sub" id="bx-arch-sub-text">从暴露面污点注入、并发变异挖掘、确定性 PoC 复现到根因诊断与补丁合成的端到端闭环</span>
                  </div>
                  <div class="bx-arch-controls">
                    <!-- Flow Mode Switcher: Bug Mining vs. Strategy Architecture -->
                    <div class="bx-arch-mode-switch">
                      <button type="button" class="bx-arch-mode-btn active" data-mode="mining" id="bx-arch-btn-mining" title="切换至 Bug 自动化挖掘与补丁全流程图">
                        <span>${BxIcons.code(13)}</span> <span>漏洞挖掘流转</span>
                      </button>
                      <button type="button" class="bx-arch-mode-btn" data-mode="strategy" id="bx-arch-btn-strategy" title="切换至攻防策略与运行时架构拓扑图">
                        <span>${BxIcons.shield(13)}</span> <span>策略拆解拓扑</span>
                      </button>
                    </div>

                    <!-- Theme Switcher: Paper Ivory vs. Cyber Dark -->
                    <div class="bx-arch-theme-switch">
                      <button type="button" class="bx-arch-theme-btn active" data-theme="paper" id="bx-arch-btn-paper" title="切换至 Alex Xu / ByteByteGo 象牙纸白风格">
                        <span class="bx-arch-dot" style="background:#faf9f5; border:1px solid #d4d4d8;"></span>
                        <span>象牙纸白</span>
                      </button>
                      <button type="button" class="bx-arch-theme-btn" data-theme="dark" id="bx-arch-btn-dark" title="切换至 BugX Studio 赛博暗黑风格">
                        <span class="bx-arch-dot" style="background:#11151c; border:1px solid #475569;"></span>
                        <span>赛博暗黑</span>
                      </button>
                    </div>
                  </div>
                </div>

                <div class="bx-arch-diagram-card theme-paper" id="bx-arch-diagram-wrap">
                  <!-- Dynamic SVG will be rendered here based on active mode -->
                </div>
              </div>
            </div>

            <!-- 3. Rounds Overview & History Card -->
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.refresh(15)} 挖掘轮次总览与历史流水 (Rounds Timeline)</span>
                <span class="bx-badge bx-badge-green" id="bx-current-round-badge"><span class="bx-status-dot active"></span> 当前轮次: Polling #1</span>
              </div>

              <div class="bx-bento" style="margin-bottom: 12px;">
                <div class="bx-bento-item">
                  <div class="bx-bento-num" id="bx-num-pollings">0</div>
                  <div class="bx-bento-label">历史挖掘轮数</div>
                </div>
                <div class="bx-bento-item">
                  <div class="bx-bento-num" id="bx-num-sessions" style="color: var(--bx-accent-cyan);">0</div>
                  <div class="bx-bento-label">审计会话数</div>
                </div>
                <div class="bx-bento-item">
                  <div class="bx-bento-num" id="bx-num-lessons" style="color: var(--bx-accent-amber);">0</div>
                  <div class="bx-bento-label">积累错题条目</div>
                </div>
                <div class="bx-bento-item">
                  <div class="bx-bento-num" id="bx-num-patches" style="color: #a78bfa;">0</div>
                  <div class="bx-bento-label">已生成补丁</div>
                </div>
              </div>

              <!-- Findings Breakdown bar -->
              <div style="font-size: 0.84rem; color: var(--bx-text-muted); margin-bottom: 12px;" id="bx-findings-text">
                正在统计 Findings 漏洞分布...
              </div>

              <!-- Rounds Table -->
              <div style="font-size: 0.84rem; font-weight: 600; margin-bottom: 6px; color: var(--bx-text-main);">历史轮次执行流水记录:</div>
              <table class="bx-table" style="font-size: 0.82rem;">
                <thead>
                  <tr>
                    <th>轮次编号</th>
                    <th>轮次 ID</th>
                    <th>执行状态</th>
                    <th>启动时间</th>
                    <th>单轮耗时</th>
                    <th>产出漏洞</th>
                  </tr>
                </thead>
                <tbody id="bx-rounds-table-body">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

          <!-- 3. Project Positioning & Attributes Profile Tab -->
          <div class="bx-tab-content" id="bx-tab-profile">
            <!-- 1. Positioning & Archetype Card -->
            <div class="bx-card bx-prof-card">
              <div class="bx-card-title bx-prof-card-title">
                <div class="bx-prof-title-group">
                  <span class="bx-prof-icon-wrap primary">${BxIcons.compass(16)}</span>
                  <div>
                    <div class="bx-prof-title-text">工程定位与架构形态</div>
                    <div class="bx-prof-title-sub">Project Positioning &amp; Architecture Archetype</div>
                  </div>
                </div>
                <span class="bx-badge bx-badge-green" id="bx-prof-scale-badge">规模: 大型复杂系统</span>
              </div>
              <div class="bx-prof-stat-grid" style="grid-template-columns: repeat(3, 1fr); margin-bottom: 14px;">
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.code(13)} 主编程语言</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-prof-primary-lang" style="color: var(--bx-accent-green);">Python</div>
                  <div class="bx-prof-stat-sub">核心技术栈支撑语言</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.layers(13)} 工程体量级别</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-prof-scale-level" style="color: var(--bx-accent-cyan);">Large</div>
                  <div class="bx-prof-stat-sub">系统代码与组件规模</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.tag(13)} 业务领域分类</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-prof-category" style="color: var(--bx-text-main); font-size: 1.05rem;">Software System</div>
                  <div class="bx-prof-stat-sub">工程业务属性定位</div>
                </div>
              </div>

              <!-- Archetypes & Attack Surfaces -->
              <div class="bx-prof-section-box">
                <div class="bx-prof-subhead">
                  <span class="bx-prof-subhead-icon" style="color: #0071e3;">${BxIcons.box(13)}</span>
                  <span>识别的工程形态定位 (Forms Archetypes):</span>
                </div>
                <div class="bx-prof-tag-group" id="bx-prof-forms"></div>
              </div>
              <div class="bx-prof-section-box" style="margin-top: 10px;">
                <div class="bx-prof-subhead">
                  <span class="bx-prof-subhead-icon" style="color: #10b981;">${BxIcons.globe(13)}</span>
                  <span>暴露的入口表面 (Attack Surfaces):</span>
                </div>
                <div class="bx-prof-tag-group" id="bx-prof-surfaces"></div>
              </div>
            </div>

            <!-- 2. Code Metrics & Scale Card -->
            <div class="bx-card bx-prof-card">
              <div class="bx-card-title bx-prof-card-title">
                <div class="bx-prof-title-group">
                  <span class="bx-prof-icon-wrap accent">${BxIcons.barChart(16)}</span>
                  <div>
                    <div class="bx-prof-title-text">代码规模与测试基线</div>
                    <div class="bx-prof-title-sub">Code Metrics &amp; Test Quality Baseline</div>
                  </div>
                </div>
              </div>
              <div class="bx-prof-stat-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 14px;">
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.fileCode(13)} 有效代码行 (SLOC)</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-metric-sloc" style="color: var(--bx-accent-green);">0</div>
                  <div class="bx-prof-stat-sub">排除空行与注释的物理源码</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.files(13)} 源码文件总数</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-metric-files" style="color: var(--bx-accent-cyan);">0</div>
                  <div class="bx-prof-stat-sub">工作区纳管的文件实体</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.alignLeft(13)} 物理总行数</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-metric-total-lines" style="color: var(--bx-accent-amber);">0</div>
                  <div class="bx-prof-stat-sub">含文档注释全部物理行数</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.shieldCheck(13)} 测试代码比率</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-metric-test-ratio" style="color: #8b5cf6;">0%</div>
                  <div class="bx-prof-stat-sub">测试代码与有效源码比率</div>
                </div>
              </div>

              <!-- Multi-language Breakdown & Stacked Bar -->
              <div class="bx-prof-section-box">
                <div class="bx-prof-subhead">
                  <span class="bx-prof-subhead-icon" style="color: #8b5cf6;">${BxIcons.pieChart(13)}</span>
                  <span>多编程语言分布与净代码构成:</span>
                </div>
                <div class="bx-prof-lang-bar" id="bx-prof-lang-bar" style="display: none;"></div>
                <div class="bx-prof-tag-group" id="bx-prof-languages"></div>
              </div>
            </div>

            <!-- 3. Engineering Economics Card -->
            <div class="bx-card bx-prof-card">
              <div class="bx-card-title bx-prof-card-title">
                <div class="bx-prof-title-group">
                  <span class="bx-prof-icon-wrap emerald">${BxIcons.calculator(16)}</span>
                  <div>
                    <div class="bx-prof-title-text">软件工程经济学估算 (COCOMO II 基线)</div>
                    <div class="bx-prof-title-sub">Constructive Cost Model &amp; Technical Debt Metrics</div>
                  </div>
                </div>
              </div>
              <div class="bx-prof-stat-grid" style="grid-template-columns: repeat(4, 1fr);">
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.user(13)} 预估工作量</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-econ-pm" style="color: var(--bx-accent-cyan);">0 PM</div>
                  <div class="bx-prof-stat-sub">Person-Months 人月工时</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.clock(13)} 预估开发周期</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-econ-months" style="color: var(--bx-accent-green);">0 月</div>
                  <div class="bx-prof-stat-sub">Schedule Months 开发周期</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.wrench(13)} 技术债务消除</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-econ-debt-days" style="color: var(--bx-accent-amber);">0 天</div>
                  <div class="bx-prof-stat-sub">Tech Debt Remediation Days</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.gauge(13)} 技术债务比率</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-econ-debt-ratio" style="color: #f43f5e;">0%</div>
                  <div class="bx-prof-stat-sub">Debt Ratio 占研发总工时</div>
                </div>
              </div>
            </div>

            <!-- 4. Quality & Risk Profile Card -->
            <div class="bx-card bx-prof-card">
              <div class="bx-card-title bx-prof-card-title">
                <div class="bx-prof-title-group">
                  <span class="bx-prof-icon-wrap rose">${BxIcons.pulse(16)}</span>
                  <div>
                    <div class="bx-prof-title-text">代码质量健康度与安全脆弱性画像</div>
                    <div class="bx-prof-title-sub">Structural Complexity, Maintainability &amp; Risk Hotspots</div>
                  </div>
                </div>
                <span class="bx-badge bx-badge-amber" id="bx-quality-grade-badge">评级: Grade B</span>
              </div>

              <!-- Quality Metrics Grid -->
              <div class="bx-prof-stat-grid" style="grid-template-columns: repeat(4, 1fr); margin-bottom: 12px;">
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.cpu(13)} 可维护性指数</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-risk-maintainability" style="color: var(--bx-text-main);">0 / 100</div>
                  <div class="bx-prof-stat-sub">Maintainability Index</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.gitBranch(13)} 平均圈复杂度</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-risk-cyclomatic" style="color: var(--bx-text-main);">0</div>
                  <div class="bx-prof-stat-sub">Avg Cyclomatic Complexity</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.copy(13)} 代码重复率</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-risk-duplication" style="color: var(--bx-text-main);">0%</div>
                  <div class="bx-prof-stat-sub">Duplication Percentage</div>
                </div>
                <div class="bx-prof-stat-card">
                  <div class="bx-prof-stat-header">
                    <span class="bx-prof-stat-label">${BxIcons.target(13)} 敏感接收沉入点</span>
                  </div>
                  <div class="bx-prof-stat-value" id="bx-risk-sinks" style="color: var(--bx-accent-amber);">0 处</div>
                  <div class="bx-prof-stat-sub">Sensitive Sinks Found</div>
                </div>
              </div>

              <!-- Threat Indicators Banner -->
              <div class="bx-prof-threat-banner" style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 14px;">
                <div class="bx-prof-threat-item">
                  <span class="bx-prof-threat-icon danger">${BxIcons.shuffle(14)}</span>
                  <div>
                    <div class="bx-prof-threat-label">污染流传递路径</div>
                    <div class="bx-prof-threat-val danger"><strong id="bx-risk-taints">0</strong> 条</div>
                  </div>
                </div>
                <div class="bx-prof-threat-item">
                  <span class="bx-prof-threat-icon danger">${BxIcons.key(14)}</span>
                  <div>
                    <div class="bx-prof-threat-label">硬编码秘密残留</div>
                    <div class="bx-prof-threat-val danger"><strong id="bx-risk-secrets">0</strong> 处</div>
                  </div>
                </div>
                <div class="bx-prof-threat-item">
                  <span class="bx-prof-threat-icon warning">${BxIcons.cube(14)}</span>
                  <div>
                    <div class="bx-prof-threat-label">巨石神模块数量</div>
                    <div class="bx-prof-threat-val warning"><strong id="bx-risk-gods">0</strong> 个</div>
                  </div>
                </div>
              </div>

              <!-- Top Hotspots Table -->
              <div class="bx-prof-subhead" style="margin-top: 14px; margin-bottom: 8px;">
                <span class="bx-prof-subhead-icon" style="color: #ff3b30;">${BxIcons.flame(14)}</span>
                <span style="font-weight: 600; color: var(--bx-text-main);">Top 高危脆弱点热点文件 (Vulnerability Hotspots):</span>
              </div>
              <table class="bx-table bx-prof-table">
                <thead>
                  <tr>
                    <th>文件路径</th>
                    <th style="width: 100px;">风险评分</th>
                    <th style="width: 90px;">代码行数</th>
                    <th>风险特征标签</th>
                  </tr>
                </thead>
                <tbody id="bx-hotspots-table-body">
                  <!-- Populated dynamically -->
                </tbody>
              </table>
            </div>
          </div>

          <!-- 4. Mounted MCP Servers & Marketplace Tab (REQ-09) -->
          <div class="bx-tab-content" id="bx-tab-mcps">
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.plug(15)} 项目挂载的 MCP 服务器与智能自动匹配 (.mcp.json + 沙箱凭据隔离)</span>
                <div style="display: flex; gap: 8px;">
                  <button type="button" class="bx-action-btn primary" id="bx-mcp-automatch-btn" title="根据项目语言、框架与攻击面智能推荐并一键挂载 MCP/Skill">
                    <span>${BxIcons.sparkles(14)}</span><span>AI 智能匹配并挂载推荐</span>
                  </button>
                </div>
              </div>
              <div id="bx-automatch-banner" style="display: none; margin-bottom: 12px; padding: 10px 14px; border-radius: 8px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); font-size: 0.8rem;"></div>
              <table class="bx-table">
                <thead>
                  <tr>
                    <th>服务名称</th>
                    <th>工具数</th>
                    <th>配置文件来源</th>
                    <th>状态与凭据沙箱</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody id="bx-mcp-table-body">
                  <tr><td colspan="5" style="text-align: center; color: var(--bx-text-muted);">暂无已挂载的 MCP</td></tr>
                </tbody>
              </table>
            </div>

            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.cube(15)} MCP 能力市场 (MCP Marketplace · 一键挂载与带外沙箱凭据配置)</span>
                <span class="bx-badge bx-badge-green" style="display: inline-flex; align-items: center; gap: 4px;">${BxIcons.lock(12)} 敏感凭据隔离于 ~/.bugx/sandboxes/</span>
              </div>
              <div class="bx-mkt-grid" id="bx-mcp-marketplace-grid">
                <!-- Populated dynamically from /api/mcp/marketplace -->
              </div>
            </div>
          </div>

          <!-- 5. Available Skills & Marketplace Tab (REQ-09) -->
          <div class="bx-tab-content" id="bx-tab-skills">
            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.zap(15)} 可用 Skill 技能包与技能市场（点击指令可填入对话，点击预览查看 SKILL.md）</span>
                <button type="button" class="bx-action-btn primary" id="bx-skill-automatch-btn">
                  <span>${BxIcons.sparkles(14)}</span><span>AI 智能匹配 Skill</span>
                </button>
              </div>
              <table class="bx-table">
                <thead>
                  <tr>
                    <th>指令词</th>
                    <th>类别</th>
                    <th>功能描述与审计职责</th>
                    <th>操作与预览</th>
                  </tr>
                </thead>
                <tbody id="bx-skill-table-body">
                  <tr><td colspan="4" style="text-align: center; color: var(--bx-text-muted);">暂无可用 Skill</td></tr>
                </tbody>
              </table>
            </div>

            <div class="bx-card" id="bx-skill-preview-card" style="display: none;">
              <div class="bx-card-title">
                <span id="bx-skill-preview-title" style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.fileCode(15)} SKILL.md 详情预览 (只读)</span>
                <div style="display: flex; gap: 8px;">
                  <button type="button" class="bx-action-btn primary" id="bx-skill-edit-ide-btn">
                    <span>${BxIcons.ide(14)}</span><span>在 IDE 中打开编辑</span>
                  </button>
                  <button type="button" class="bx-action-btn" onclick="document.getElementById('bx-skill-preview-card').style.display='none'">关闭</button>
                </div>
              </div>
              <pre class="bx-doc-pre" id="bx-skill-preview-content" style="max-height: 280px; overflow-y: auto;"></pre>
            </div>

            <div class="bx-card">
              <div class="bx-card-title">
                <span style="display: inline-flex; align-items: center; gap: 8px;">${BxIcons.cube(15)} Skill 技能包市场 (Skill Marketplace · 支持项目级启停与挂载)</span>
              </div>
              <div class="bx-mkt-grid" id="bx-skill-marketplace-grid">
                <!-- Populated dynamically from /api/skills/marketplace -->
              </div>
            </div>
          </div>

          <!-- 6. Read-Only Project Document Center Tab (REQ-05) -->
          <div class="bx-tab-content" id="bx-tab-docs">
            <div class="bx-card">
              <div class="bx-card-title">
                <span>${BxIcons.book(15)} 项目文档与配置中心 (只读渲染 · 全文检索 · 一键唤起本地 IDE 编辑)</span>
                <div style="display: flex; align-items: center; gap: 8px;">
                  <input type="text" id="bx-doc-search-input" class="bx-proj-select" style="font-size: 0.78rem; width: 220px;" placeholder="快速过滤文档与配置内容..." />
                  <button type="button" class="bx-action-btn" id="bx-doc-search-btn">${BxIcons.search(13)} 搜索</button>
                  <button type="button" class="bx-action-btn primary" id="bx-doc-open-ide-btn" title="调用本地 IDE 打开当前选中的文档进行编辑 (REQ-04 + REQ-05)">
                    <span>${BxIcons.ide(14)}</span><span>在 IDE 中编辑当前文档</span>
                  </button>
                </div>
              </div>
              <div class="bx-docs-layout">
                <div class="bx-docs-sidebar">
                  <div style="font-size: 0.76rem; font-weight: 600; color: var(--bx-text-muted); display: inline-flex; align-items: center; gap: 4px;">${BxIcons.folder(14)} <span>受管文档与配置树 (<span id="bx-doc-count">0</span>)</span></div>
                  <div class="bx-docs-list" id="bx-docs-list">
                    <!-- Populated from /api/docs/tree or /api/docs/search -->
                  </div>
                </div>
                <div class="bx-docs-viewer">
                  <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid var(--bx-border-subtle);">
                    <strong id="bx-doc-current-path" style="font-family: monospace; font-size: 0.84rem; color: var(--bx-accent-cyan);">请选择左侧文档查看内容</strong>
                    <span class="bx-badge bx-badge-gray" id="bx-doc-readonly-badge">${BxIcons.shield(12)} Web 端只读保护</span>
                  </div>
                  <pre class="bx-doc-pre" id="bx-doc-content-box">点击左侧任意 Markdown (.md)、文本 (.txt)、架构图 (.mermaid) 或配置文件 (.json/.yaml/.toml) 即可在此实时预览。</pre>
                </div>
              </div>
            </div>
          </div>

          <!-- 7. Workspace Files Management Tab -->
          <div class="bx-tab-content" id="bx-tab-files">
            <div class="bx-card">
              <div class="bx-card-title">
                <span>${BxIcons.files(15)} 工作区文件治理 (实时浏览 · 快速过滤 · 安全删除与新建)</span>
                <div style="display: flex; align-items: center; gap: 8px;">
                  <input type="text" id="bx-ws-files-search" class="bx-proj-select" style="font-size: 0.78rem; width: 200px;" placeholder="快速过滤文件名..." />
                  <button type="button" class="bx-action-btn" id="bx-ws-files-refresh-btn" title="刷新文件列表">${BxIcons.refresh(13)} 刷新</button>
                  <button type="button" class="bx-action-btn primary" id="bx-ws-files-mkdir-btn" title="在当前目录下新建子文件夹">
                    <span>${BxIcons.folderPlus(13)}</span><span>新建文件夹</span>
                  </button>
                </div>
              </div>
              <div class="bx-files-layout">
                <div class="bx-files-breadcrumb-bar" id="bx-ws-files-breadcrumb">
                  <!-- Breadcrumbs populated dynamically -->
                </div>
                <div class="bx-files-container">
                  <table class="bx-files-table">
                    <thead>
                      <tr>
                        <th style="width: 45%;">名称</th>
                        <th style="width: 15%;">类型</th>
                        <th style="width: 15%;">大小</th>
                        <th style="width: 25%; text-align: right;">操作</th>
                      </tr>
                    </thead>
                    <tbody id="bx-ws-files-tbody">
                      <!-- File & Directory rows populated dynamically -->
                    </tbody>
                  </table>
                  <div id="bx-ws-files-empty" class="bx-empty-note" style="display: none; padding: 24px; text-align: center; color: var(--bx-text-muted);">
                    暂无文件或目录
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;

    document.body.appendChild(overlay);

    // Bind Close & Tabs
    document.getElementById('bx-modal-close-btn').onclick = closeSettingsModal;
    overlay.onclick = (e) => {
      if (e.target === overlay) closeSettingsModal();
    };

    document.querySelectorAll('.bx-tab-btn').forEach(btn => {
      btn.onclick = () => {
        document.querySelectorAll('.bx-tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.bx-tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        const tabKey = btn.getAttribute('data-tab');
        const pane = document.getElementById('bx-tab-' + tabKey);
        if (pane) pane.classList.add('active');

        // Tab 切换时控制实时动态图表轮询与懒加载数据
        if (tabKey === 'mining') {
          startLiveMonitoring();
          renderRadarChart((currentSettings && currentSettings.mining && currentSettings.mining.chart_data) || {}, (currentSettings && currentSettings.mining && currentSettings.mining.strategy) || {});
          if (currentSettings) {
            renderMiningTab(currentSettings.mining || {});
            renderStrategyFlowDiagram(currentSettings.mining || {}, currentSettings);
          } else {
            refreshMiningDataOnly(true);
          }
        } else {
          stopLiveMonitoring();
        }
        if (tabKey === 'mcps') {
          loadMcpMarketplace();
        } else if (tabKey === 'skills') {
          loadSkillMarketplace();
        } else if (tabKey === 'docs') {
          loadProjectDocsTree();
        } else if (tabKey === 'files') {
          loadWorkspaceFiles();
        }
      };
    });

    // 绑定 LIVE 监控控制条与手动取消任务按钮 (REQ-08)
    const refreshBtn = document.getElementById('bx-live-refresh-btn');
    if (refreshBtn) {
      refreshBtn.onclick = () => refreshMiningDataOnly(true);
    }
    const cancelMiningBtn = document.getElementById('bx-cancel-mining-btn');
    if (cancelMiningBtn) {
      cancelMiningBtn.onclick = () => cancelActiveProjectTask();
    }
    const intervalSelect = document.getElementById('bx-live-interval-select');
    if (intervalSelect) {
      intervalSelect.onchange = () => startLiveMonitoring();
    }

    // Bind Select Project change & Show Archived checkbox (REQ-06)
    document.getElementById('bx-proj-selector').onchange = (e) => {
      loadProjectSettings(e.target.value);
    };
    const showArchCb = document.getElementById('bx-show-archived-cb');
    if (showArchCb) {
      showArchCb.onchange = () => fetchProjectsList().then(() => loadProjectSettings(currentHash));
    }

    // Bind Header IDE Open, Archive, Delete buttons (REQ-04, REQ-06)
    const openIdeBtn = document.getElementById('bx-open-ide-btn');
    if (openIdeBtn) {
      openIdeBtn.onclick = () => openInSelectedIDE('');
    }
    const archiveBtn = document.getElementById('bx-archive-proj-btn');
    if (archiveBtn) {
      archiveBtn.onclick = () => toggleArchiveCurrentProject();
    }
    const deleteBtn = document.getElementById('bx-delete-proj-btn');
    if (deleteBtn) {
      deleteBtn.onclick = () => promptDeleteCurrentProject();
    }

    // Bind Model Config Sliders (REQ-03)
    const tempSlider = document.getElementById('bx-mc-temperature');
    const tempVal = document.getElementById('bx-mc-temp-val');
    if (tempSlider && tempVal) {
      tempSlider.oninput = (e) => {
        tempVal.innerText = Number(e.target.value).toFixed(2);
        updateGateBadges(true);
      };
    }
    const topPSlider = document.getElementById('bx-mc-top-p');
    const topPVal = document.getElementById('bx-mc-topp-val');
    if (topPSlider && topPVal) {
      topPSlider.oninput = (e) => {
        topPVal.innerText = Number(e.target.value).toFixed(2);
        updateGateBadges(true);
      };
    }
    const concSlider = document.getElementById('bx-mc-concurrency');
    const concVal = document.getElementById('bx-mc-conc-val');
    if (concSlider && concVal) {
      concSlider.oninput = (e) => {
        concVal.innerText = `${e.target.value} Workers`;
        updateGateBadges(true);
      };
    }

    // Bind Auto-Match Buttons (REQ-09) & Session History Refresh (REQ-02)
    const mcpAutoBtn = document.getElementById('bx-mcp-automatch-btn');
    if (mcpAutoBtn) mcpAutoBtn.onclick = () => runCapabilityAutoMatch(true);
    const skillAutoBtn = document.getElementById('bx-skill-automatch-btn');
    if (skillAutoBtn) skillAutoBtn.onclick = () => runCapabilityAutoMatch(true);
    const refreshSessBtn = document.getElementById('bx-refresh-sessions-btn');
    if (refreshSessBtn) refreshSessBtn.onclick = () => loadProjectSessions();

    // Bind Document Center Search & IDE Edit (REQ-05)
    const docSearchBtn = document.getElementById('bx-doc-search-btn');
    const docSearchInput = document.getElementById('bx-doc-search-input');
    if (docSearchBtn && docSearchInput) {
      docSearchBtn.onclick = () => searchProjectDocs(docSearchInput.value);
      docSearchInput.onkeydown = (e) => {
        if (e.key === 'Enter') searchProjectDocs(docSearchInput.value);
      };
    }
    const docOpenIdeBtn = document.getElementById('bx-doc-open-ide-btn');
    if (docOpenIdeBtn) {
      docOpenIdeBtn.onclick = () => {
        const curDoc = window._bxCurrentDocPath || '';
        openInSelectedIDE(curDoc);
      };
    }

    // Bind Workspace Files Center (Search, Refresh, Mkdir)
    const wsFilesSearch = document.getElementById('bx-ws-files-search');
    if (wsFilesSearch) {
      wsFilesSearch.oninput = (e) => filterWorkspaceFiles(e.target.value);
    }
    const wsFilesRefreshBtn = document.getElementById('bx-ws-files-refresh-btn');
    if (wsFilesRefreshBtn) {
      wsFilesRefreshBtn.onclick = () => loadWorkspaceFiles(currentWsFilesPath);
    }
    const wsFilesMkdirBtn = document.getElementById('bx-ws-files-mkdir-btn');
    if (wsFilesMkdirBtn) {
      wsFilesMkdirBtn.onclick = () => promptCreateWorkspaceFolder(currentWsFilesPath);
    }

    // Bind 5 Gates Form Elements & Sync
    const autoFixToggle = document.getElementById('bx-toggle-autofix');
    const fixModeSelect = document.getElementById('bx-select-fix-mode');
    // 1. 修复模式双向绑定联动
    autoFixToggle.onchange = (e) => {
      if (e.target.checked) {
        fixModeSelect.value = 'auto';
      } else {
        fixModeSelect.value = 'off';
      }
      updateGateBadges(true);
    };
    fixModeSelect.onchange = (e) => {
      autoFixToggle.checked = (e.target.value !== 'off');
      updateGateBadges(true);
    };

    // 2. 云端同步双向绑定联动
    const cloudSyncToggle = document.getElementById('bx-toggle-cloudsync');
    const cloudPolicySelect = document.getElementById('bx-select-cloud-policy');
    cloudSyncToggle.onchange = (e) => {
      if (!e.target.checked) {
        cloudPolicySelect.value = 'local';
      } else {
        cloudPolicySelect.value = 'authorized';
      }
      updateGateBadges(true);
    };
    cloudPolicySelect.onchange = (e) => {
      cloudSyncToggle.checked = (e.target.value !== 'block' && e.target.value !== 'local');
      updateGateBadges(true);
    };

    document.getElementById('bx-select-review').onchange = () => updateGateBadges(true);
    document.getElementById('bx-select-polling').onchange = () => updateGateBadges(true);
    document.getElementById('bx-select-commit').onchange = () => updateGateBadges(true);

    // Explicit Save Button
    document.getElementById('bx-save-all-gates-btn').onclick = saveAllGates;

    // Esc key close
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && overlay.classList.contains('active')) {
        closeSettingsModal();
      }
    });

    // Initial baseline render for radar chart so it is never empty
    renderRadarChart({}, {});
  }

  function updateGateBadges(isDirty = false) {
    const autoFixCb = document.getElementById('bx-toggle-autofix');
    const fixModeVal = document.getElementById('bx-select-fix-mode').value;
    if (autoFixCb) {
      autoFixCb.checked = (fixModeVal !== 'off');
    }
    const autoFixBadge = document.getElementById('bx-autofix-badge');
    if (autoFixBadge) {
      if (fixModeVal === 'auto') {
        autoFixBadge.innerHTML = '<span class="bx-status-dot active"></span> 自动合入';
        autoFixBadge.className = 'bx-badge bx-badge-green';
      } else if (fixModeVal === 'confirm') {
        autoFixBadge.innerHTML = '<span class="bx-status-dot warning"></span> 待用户批准';
        autoFixBadge.className = 'bx-badge bx-badge-amber';
      } else {
        autoFixBadge.innerHTML = '<span class="bx-status-dot inactive"></span> 仅报告 (已关闭)';
        autoFixBadge.className = 'bx-badge bx-badge-gray';
      }
    }

    const cloudPolicyVal = document.getElementById('bx-select-cloud-policy').value;
    const cloudSyncToggle = document.getElementById('bx-toggle-cloudsync');
    if (cloudSyncToggle) {
      cloudSyncToggle.checked = (cloudPolicyVal !== 'block' && cloudPolicyVal !== 'local');
    }
    const cloudSyncBadge = document.getElementById('bx-cloudsync-badge');
    if (cloudSyncBadge) {
      if (cloudPolicyVal === 'block') {
        cloudSyncBadge.innerHTML = '<span class="bx-status-dot danger"></span> 强制阻断';
        cloudSyncBadge.className = 'bx-badge bx-badge-gray';
      } else if (cloudPolicyVal === 'local') {
        cloudSyncBadge.innerHTML = '<span class="bx-status-dot inactive"></span> 纯本地模式';
        cloudSyncBadge.className = 'bx-badge bx-badge-gray';
      } else if (currentSettings && currentSettings.gates && currentSettings.gates.cloud_project_id) {
        cloudSyncBadge.innerHTML = `<span class="bx-status-dot active"></span> 已绑定 (ID: ${currentSettings.gates.cloud_project_id})`;
        cloudSyncBadge.className = 'bx-badge bx-badge-green';
      } else {
        cloudSyncBadge.innerHTML = '<span class="bx-status-dot active"></span> 开启同步 (待关联)';
        cloudSyncBadge.className = 'bx-badge bx-badge-green';
      }
    }

    const reviewVal = document.getElementById('bx-select-review').value;
    const reviewBadge = document.getElementById('bx-review-badge');
    if (reviewBadge) {
      const isAuto = reviewVal === 'auto';
      const isInter = reviewVal === 'interactive';
      reviewBadge.innerHTML = `<span class="bx-status-dot ${isAuto ? 'active' : (isInter ? 'warning' : 'inactive')}"></span> ${isAuto ? '全自动' : (isInter ? '交互确认' : '已关闭')}`;
      reviewBadge.className = 'bx-badge ' + (isAuto ? 'bx-badge-green' : (isInter ? 'bx-badge-amber' : 'bx-badge-gray'));
    }

    const pollingVal = document.getElementById('bx-select-polling').value;
    const pollingBadge = document.getElementById('bx-polling-badge');
    if (pollingBadge) {
      const isAuto = pollingVal === 'auto';
      const isManual = pollingVal === 'manual';
      pollingBadge.innerHTML = `<span class="bx-status-dot ${isAuto ? 'active' : (isManual ? 'warning' : 'inactive')}"></span> ${isAuto ? '持续轮询' : (isManual ? '单轮手动' : '已关闭')}`;
      pollingBadge.className = 'bx-badge ' + (isAuto ? 'bx-badge-green' : (isManual ? 'bx-badge-amber' : 'bx-badge-gray'));
    }

    const commitVal = document.getElementById('bx-select-commit').value;
    const commitBadge = document.getElementById('bx-commit-badge');
    if (commitBadge) {
      const isOff = commitVal === 'off';
      commitBadge.innerHTML = `<span class="bx-status-dot ${isOff ? 'inactive' : 'active'}"></span> ${commitVal === 'round' ? '每轮提交' : (commitVal === 'task' ? '按任务提交' : '已关闭')}`;
      commitBadge.className = 'bx-badge ' + (!isOff ? 'bx-badge-cyan' : 'bx-badge-gray');
    }

    const statusText = document.getElementById('bx-save-status-text');
    if (statusText) {
      if (isDirty) {
        statusText.innerHTML = `${BxIcons.alert(13)} 发现未保存的门禁变更，请点击右侧按钮保存并同步至磁盘！`;
        statusText.style.color = 'var(--bx-accent-amber)';
      } else {
        statusText.innerHTML = `${BxIcons.check(13)} 当前配置已与磁盘保持一致`;
        statusText.style.color = 'var(--bx-accent-green)';
      }
    }
  }

  function removeUnwantedTriggers() {
    // 移除右下角“项目配置与门禁”悬浮按钮 (REQ: 删除)
    const trigger = document.getElementById('bx-settings-trigger-btn');
    if (trigger) trigger.remove();

    // 移除侧栏底部及折叠导轨手机图标（远程访问） (REQ: 删除图标)
    document.querySelectorAll('.sidebar-icon-btn, .rail-btn').forEach(btn => {
      const title = btn.getAttribute('title') || '';
      const label = btn.getAttribute('aria-label') || '';
      if (title.includes('远程') || title.includes('Remote') || label.includes('远程') || label.includes('Remote') || btn.querySelector('rect[x="4.5"]')) {
        btn.remove();
      }
    });
  }

  async function fetchProjectsList() {
    try {
      const showArch = document.getElementById('bx-show-archived-cb')?.checked;
      const url = showArch ? '/projects?include_archived=1' : '/projects';
      const res = await fetch(url);
      if (res.ok) {
        projectsList = await res.json();
        const sel = document.getElementById('bx-proj-selector');
        if (sel) {
          sel.innerHTML = projectsList.map(p => {
            const archTag = p.archived ? ' [已归档]' : '';
            const pctTag = (p.mining_progress && p.mining_progress.coverage_pct !== undefined) ? ` · ${p.mining_progress.coverage_pct}%` : '';
            return `<option value="${p.hash}">${p.name}${archTag} (${p.session_count || 1}轮${pctTag})</option>`;
          }).join('');
        }
      }
      // Populate installed IDEs / AI tools (REQ-04)
      const ideRes = await fetch('/api/ide/list');
      if (ideRes.ok) {
        const ideData = await ideRes.json();
        const ideSel = document.getElementById('bx-ide-selector');
        if (ideSel && Array.isArray(ideData.tools)) {
          const installed = ideData.tools.filter(t => t.installed);
          const list = installed.length ? installed : ideData.tools;
          ideSel.innerHTML = list.map(t => `<option value="${t.id}">${t.name}${t.installed ? '' : ' (URI)'}</option>`).join('');
        }
      }
    } catch (e) {
      console.warn('[ProjectSettings] fetch projects failed:', e);
    }
  }

  // =========================================================================
  // Live Real-Time Mining Monitor & Dynamic Visual Charts (Build 152)
  // =========================================================================
  let liveTimer = null;
  let liveIntervalSec = 3;

  function stopLiveMonitoring() {
    if (liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
    const dot = document.querySelector('.bx-live-pulse-dot');
    if (dot) dot.style.animationPlayState = 'paused';
    const ecg = document.querySelector('.bx-ecg-path');
    if (ecg) ecg.style.animationPlayState = 'paused';
  }

  function startLiveMonitoring() {
    stopLiveMonitoring();
    const intervalSelect = document.getElementById('bx-live-interval-select');
    liveIntervalSec = intervalSelect ? parseInt(intervalSelect.value, 10) : 3;
    if (liveIntervalSec <= 0) return;

    const dot = document.querySelector('.bx-live-pulse-dot');
    if (dot) dot.style.animationPlayState = 'running';
    const ecg = document.querySelector('.bx-ecg-path');
    if (ecg) ecg.style.animationPlayState = 'running';

    liveTimer = setInterval(async () => {
      const overlay = document.getElementById('bx-settings-overlay');
      const miningTab = document.getElementById('bx-tab-mining');
      if (!overlay || !overlay.classList.contains('active') || !miningTab || !miningTab.classList.contains('active')) {
        stopLiveMonitoring();
        return;
      }
      await refreshMiningDataOnly(false);
    }, liveIntervalSec * 1000);
  }

  async function refreshMiningDataOnly(showSpin = false) {
    const refreshBtn = document.getElementById('bx-live-refresh-btn');
    if (showSpin && refreshBtn) refreshBtn.classList.add('spinning');
    try {
      const endpoint = currentHash ? `/projects/${currentHash}/settings` : `/project/settings`;
      const res = await fetch(endpoint);
      if (res.ok) {
        const data = await res.json();
        currentSettings = data;
        renderMiningTab(data.mining || {});
        const infoEl = document.getElementById('bx-live-round-info');
        if (infoEl) {
          const nowStr = new Date().toLocaleTimeString();
          infoEl.innerText = `最近采样: ${nowStr} · 信号流正常`;
        }
      }
    } catch (e) {
      console.warn('[ProjectSettings] live polling failed:', e);
    } finally {
      if (showSpin && refreshBtn) {
        setTimeout(() => refreshBtn.classList.remove('spinning'), 500);
      }
    }
  }

  function renderFlowingPipeline(prog) {
    const box = document.getElementById('bx-flowing-pipeline-box');
    if (!box) return;

    const pIdx = prog.phase_index || 0;
    const steps = prog.pipeline_steps || [
      { id: 1, title: '架构与攻击面扫描', subtitle: 'AST & Surface Probe', status: pIdx > 1 ? 'done' : (pIdx === 1 ? 'active' : 'pending') },
      { id: 2, title: '漏洞挖掘与差异验证', subtitle: 'Vuln Hunt & Diff', status: pIdx > 2 ? 'done' : (pIdx === 2 ? 'active' : 'pending') },
      { id: 3, title: '防御补丁自动合成', subtitle: 'Patch Synthesis', status: pIdx > 3 ? 'done' : (pIdx === 3 ? 'active' : 'pending') },
      { id: 4, title: '沙箱双轨回归验证', subtitle: 'Sandbox Dual-Verification', status: pIdx >= 4 ? 'done' : 'pending' },
    ];

    let html = '';
    steps.forEach((st, idx) => {
      const isDone = st.status === 'done';
      const isActive = st.status === 'active';
      const nodeClass = `bx-pipeline-node ${isDone ? 'done' : (isActive ? 'active' : '')}`;
      const badgeText = isDone ? '✓ 完成' : (isActive ? '● 执行中' : '等待');

      html += `
        <div class="${nodeClass}" title="${st.subtitle}">
          <div class="bx-node-header">
            <span class="bx-node-badge">${badgeText}</span>
            <span style="font-size: 0.7rem; color: var(--bx-text-muted);">Step ${st.id}</span>
          </div>
          <div class="bx-node-title">${st.title}</div>
          <div class="bx-node-sub">${st.subtitle}</div>
        </div>
      `;

      if (idx < steps.length - 1) {
        const nextActive = steps[idx + 1].status === 'active' || isActive;
        html += `
          <div class="bx-pipeline-link">
            <svg class="bx-flow-svg" viewBox="0 0 32 20">
              <line class="bx-flow-track" x1="0" y1="10" x2="32" y2="10" />
              <line class="bx-flow-particles" x1="0" y1="10" x2="32" y2="10" style="stroke: ${isDone ? '#06b6d4' : (nextActive ? '#10b981' : 'rgba(255,255,255,0.15)')}; animation-duration: ${nextActive ? '0.7s' : '1.8s'};" />
            </svg>
          </div>
        `;
      }
    });

    box.innerHTML = html;
  }

  function renderRadialGauge(coveragePct) {
    const circle = document.getElementById('bx-gauge-val-circle');
    if (!circle) return;
    const clamped = Math.min(100, Math.max(0, coveragePct || 0));
    const circumference = 163.36;
    const offset = circumference * (1 - clamped / 100);
    circle.style.strokeDashoffset = offset;
  }

  function renderRoundTrendChart(chartData, rounds) {
    const svg = document.getElementById('bx-trend-svg');
    if (!svg) return;

    let series = (chartData && chartData.trend_series && chartData.trend_series.length) ? chartData.trend_series : [];
    if (!series.length && rounds && rounds.length) {
      series = [...rounds].sort((a, b) => (a.polling_no || 0) - (b.polling_no || 0));
    }

    const gridGroup = document.getElementById('bx-trend-grid');
    const areaPath = document.getElementById('bx-trend-area');
    const lineElapsed = document.getElementById('bx-trend-line-elapsed');
    const lineFindings = document.getElementById('bx-trend-line-findings');
    const dotsGroup = document.getElementById('bx-trend-dots');
    const tooltip = document.getElementById('bx-trend-tooltip');
    const crosshair = document.getElementById('bx-trend-crosshair');

    if (gridGroup) gridGroup.innerHTML = '';
    if (dotsGroup) dotsGroup.innerHTML = '';

    const W = 460;
    const H = 180;
    const padL = 36;
    const padR = 24;
    const padT = 20;
    const padB = 28;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    // 绘制横向网格线
    [0, 0.5, 1].forEach(ratio => {
      const y = padT + plotH * (1 - ratio);
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('class', 'bx-grid-line');
      line.setAttribute('x1', padL);
      line.setAttribute('x2', W - padR);
      line.setAttribute('y1', y);
      line.setAttribute('y2', y);
      if (gridGroup) gridGroup.appendChild(line);
    });

    if (series.length === 0) {
      if (areaPath) areaPath.setAttribute('d', '');
      if (lineElapsed) lineElapsed.setAttribute('d', '');
      if (lineFindings) lineFindings.setAttribute('d', '');
      const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      txt.setAttribute('x', W / 2);
      txt.setAttribute('y', H / 2 + 5);
      txt.setAttribute('text-anchor', 'middle');
      txt.setAttribute('fill', '#9ca3af');
      txt.setAttribute('font-size', '11');
      txt.textContent = '暂无轮次历史 · 运行 /audit 开始实时波形绘制';
      if (gridGroup) gridGroup.appendChild(txt);
      return;
    }

    const maxElapsed = Math.max(...series.map(s => s.elapsed_seconds || 0), 10);
    const maxFindings = Math.max(...series.map(s => s.findings_count || 0), 4);

    // Y 轴刻度文字
    const lblTopLeft = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    lblTopLeft.setAttribute('class', 'bx-axis-label');
    lblTopLeft.setAttribute('x', padL - 6);
    lblTopLeft.setAttribute('y', padT + 4);
    lblTopLeft.setAttribute('text-anchor', 'end');
    lblTopLeft.textContent = Math.round(maxElapsed) + 's';
    if (gridGroup) gridGroup.appendChild(lblTopLeft);

    const lblTopRight = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    lblTopRight.setAttribute('class', 'bx-axis-label');
    lblTopRight.setAttribute('x', W - padR + 6);
    lblTopRight.setAttribute('y', padT + 4);
    lblTopRight.setAttribute('text-anchor', 'start');
    lblTopRight.textContent = maxFindings + '个';
    if (gridGroup) gridGroup.appendChild(lblTopRight);

    // 计算各点坐标
    const points = series.map((s, idx) => {
      const x = series.length === 1 
        ? padL + plotW / 2 
        : padL + (idx / (series.length - 1)) * plotW;
      const yElapsed = padT + plotH - ((s.elapsed_seconds || 0) / maxElapsed) * plotH;
      const yFindings = padT + plotH - ((s.findings_count || 0) / maxFindings) * plotH;
      return { x, yElapsed, yFindings, data: s, idx };
    });

    // 绘制 X 轴轮次标签
    points.forEach(pt => {
      const xLbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      xLbl.setAttribute('class', 'bx-axis-label');
      xLbl.setAttribute('x', pt.x);
      xLbl.setAttribute('y', H - 8);
      xLbl.setAttribute('text-anchor', 'middle');
      xLbl.textContent = `#${pt.data.polling_no || (pt.idx + 1)}`;
      if (gridGroup) gridGroup.appendChild(xLbl);
    });

    // 贝塞尔平滑路径
    function buildSmoothPath(pts, yKey) {
      if (pts.length === 1) {
        return `M ${padL} ${pts[0][yKey]} L ${pts[0].x} ${pts[0][yKey]} L ${W - padR} ${pts[0][yKey]}`;
      }
      let d = `M ${pts[0].x} ${pts[0][yKey]}`;
      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[i];
        const p1 = pts[i + 1];
        const cpX = (p0.x + p1.x) / 2;
        d += ` C ${cpX} ${p0[yKey]}, ${cpX} ${p1[yKey]}, ${p1.x} ${p1[yKey]}`;
      }
      return d;
    }

    const elapsedLineD = buildSmoothPath(points, 'yElapsed');
    const findingsLineD = buildSmoothPath(points, 'yFindings');

    let areaD = '';
    if (points.length === 1) {
      areaD = `M ${padL} ${points[0].yElapsed} L ${W - padR} ${points[0].yElapsed} L ${W - padR} ${padT + plotH} L ${padL} ${padT + plotH} Z`;
    } else {
      areaD = elapsedLineD + ` L ${points[points.length - 1].x} ${padT + plotH} L ${points[0].x} ${padT + plotH} Z`;
    }

    if (areaPath) areaPath.setAttribute('d', areaD);
    if (lineElapsed) lineElapsed.setAttribute('d', elapsedLineD);
    if (lineFindings) lineFindings.setAttribute('d', findingsLineD);

    // 绘制数据点
    points.forEach(pt => {
      const c1 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c1.setAttribute('class', 'bx-chart-dot');
      c1.setAttribute('cx', pt.x);
      c1.setAttribute('cy', pt.yElapsed);
      c1.setAttribute('r', '4');
      c1.setAttribute('fill', '#06b6d4');
      c1.setAttribute('stroke', '#083344');
      c1.setAttribute('stroke-width', '2');
      if (dotsGroup) dotsGroup.appendChild(c1);

      const c2 = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c2.setAttribute('class', 'bx-chart-dot');
      c2.setAttribute('cx', pt.x);
      c2.setAttribute('cy', pt.yFindings);
      c2.setAttribute('r', '4');
      c2.setAttribute('fill', '#f97316');
      c2.setAttribute('stroke', '#431407');
      c2.setAttribute('stroke-width', '2');
      if (dotsGroup) dotsGroup.appendChild(c2);
    });

    // 交互悬停十字光标与 Tooltip
    svg.onmousemove = (e) => {
      const rect = svg.getBoundingClientRect();
      const mouseX = ((e.clientX - rect.left) / rect.width) * W;
      let closest = points[0];
      let minDis = Math.abs(mouseX - closest.x);
      for (let i = 1; i < points.length; i++) {
        const dis = Math.abs(mouseX - points[i].x);
        if (dis < minDis) {
          minDis = dis;
          closest = points[i];
        }
      }

      if (crosshair) {
        crosshair.style.display = 'block';
        crosshair.setAttribute('x1', closest.x);
        crosshair.setAttribute('x2', closest.x);
      }

      if (tooltip) {
        const wrapRect = document.getElementById('bx-trend-chart-wrap').getBoundingClientRect();
        const screenX = (closest.x / W) * wrapRect.width;
        const screenY = (closest.yElapsed / H) * wrapRect.height;
        tooltip.style.left = `${screenX}px`;
        tooltip.style.top = `${screenY}px`;
        tooltip.innerHTML = `
          <div style="font-weight:700; color:#38bdf8; margin-bottom:2px;">第 ${closest.data.polling_no || 1} 轮 · ${closest.data.polling_id || ''}</div>
          <div style="display:flex; align-items:center; gap:5px;">${BxIcons.clock(12)} 耗时: <strong>${closest.data.elapsed_display || closest.data.elapsed_seconds + 's'}</strong></div>
          <div style="display:flex; align-items:center; gap:5px;">${BxIcons.bug(12)} 漏洞发现: <strong style="color:#f97316;">${closest.data.findings_count || 0} 个</strong></div>
          <div style="color:#9ca3af; font-size:0.7rem; margin-top:2px; display:flex; align-items:center; gap:5px;">${BxIcons.calendar(11)} ${closest.data.created_at || '未知时间'}</div>
        `;
        tooltip.classList.add('show');
      }
    };

    svg.onmouseleave = () => {
      if (crosshair) crosshair.style.display = 'none';
      if (tooltip) tooltip.classList.remove('show');
    };
  }

  function renderRadarChart(chartData, strat) {
    const gridLayer = document.getElementById('bx-radar-grid-layer');
    const pointsLayer = document.getElementById('bx-radar-points-layer');
    const labelsLayer = document.getElementById('bx-radar-labels-layer');
    const poly = document.getElementById('bx-radar-polygon');
    const tooltip = document.getElementById('bx-radar-tooltip');
    const metricsBar = document.getElementById('bx-radar-metrics-bar');
    if (!gridLayer || !poly) return;

    gridLayer.innerHTML = '';
    pointsLayer.innerHTML = '';
    labelsLayer.innerHTML = '';

    const cx = 170;
    const cy = 110;
    const R = 68;
    const dims = (chartData && chartData.radar_dimensions) ? chartData.radar_dimensions : [
      { key: 'api', label: 'API 接口 (API)', score: 88 },
      { key: 'auth', label: '认证权限 (Auth)', score: 82 },
      { key: 'file', label: '文件系统 (File)', score: 78 },
      { key: 'ui', label: '前端交互 (UI)', score: 25 },
      { key: 'storage', label: '数据存储 (DB)', score: 80 },
      { key: 'queue', label: '消息任务 (Queue)', score: 20 },
    ];

    const numAxes = dims.length;
    const angleStep = (Math.PI * 2) / numAxes;

    // 1. 绘制 5 层同心多边形网格与刻度标尺
    const levels = [0.2, 0.4, 0.6, 0.8, 1.0];
    levels.forEach((level, lIdx) => {
      const pts = [];
      for (let i = 0; i < numAxes; i++) {
        const a = i * angleStep - Math.PI / 2;
        const x = cx + R * level * Math.cos(a);
        const y = cy + R * level * Math.sin(a);
        pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      }
      const pLine = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
      pLine.setAttribute('points', pts.join(' '));
      pLine.setAttribute('class', `bx-radar-grid-ring ${level === 1.0 ? 'outer' : 'dash'}`);
      gridLayer.appendChild(pLine);

      // 在垂直顶部轴线上标记刻度百分比 (40%, 80%, 100%)
      if (level === 0.4 || level === 0.8 || level === 1.0) {
        const scaleTxt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        scaleTxt.setAttribute('class', 'bx-radar-scale-text');
        scaleTxt.setAttribute('x', (cx + 3).toFixed(1));
        scaleTxt.setAttribute('y', (cy - R * level + 8).toFixed(1));
        scaleTxt.textContent = `${Math.round(level * 100)}%`;
        gridLayer.appendChild(scaleTxt);
      }
    });

    // 2. 绘制 6 条放射轴线与中心圆核
    for (let i = 0; i < numAxes; i++) {
      const a = i * angleStep - Math.PI / 2;
      const x = cx + R * Math.cos(a);
      const y = cy + R * Math.sin(a);
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', cx);
      line.setAttribute('y1', cy);
      line.setAttribute('x2', x.toFixed(1));
      line.setAttribute('y2', y.toFixed(1));
      line.setAttribute('class', 'bx-radar-grid-axis');
      gridLayer.appendChild(line);
    }

    const centerHub = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    centerHub.setAttribute('cx', cx);
    centerHub.setAttribute('cy', cy);
    centerHub.setAttribute('r', '2.8');
    centerHub.setAttribute('class', 'bx-radar-center-hub');
    gridLayer.appendChild(centerHub);

    // 3. 维度文本标签与分值徽标布局 (精准规避边缘碰撞)
    for (let i = 0; i < numAxes; i++) {
      const d = dims[i];
      const mainName = d.label.replace(/\s*\(.*?\)/, '').trim();
      const score = Math.round(d.score || 0);
      const scoreClass = score >= 80 ? 'good' : (score >= 60 ? 'normal' : 'warn');

      let lx = cx;
      let ly = cy;
      let anchor = 'middle';

      if (i === 0) {
        // 顶部 (API)
        lx = cx;
        ly = cy - R - 10;
        anchor = 'middle';
      } else if (i === 1) {
        // 右上 (Auth)
        lx = cx + R * 0.88 + 14;
        ly = cy - R * 0.48;
        anchor = 'start';
      } else if (i === 2) {
        // 右下 (File)
        lx = cx + R * 0.88 + 14;
        ly = cy + R * 0.48 + 4;
        anchor = 'start';
      } else if (i === 3) {
        // 底部 (UI)
        lx = cx;
        ly = cy + R + 18;
        anchor = 'middle';
      } else if (i === 4) {
        // 左下 (DB)
        lx = cx - R * 0.88 - 14;
        ly = cy + R * 0.48 + 4;
        anchor = 'end';
      } else if (i === 5) {
        // 左上 (Queue)
        lx = cx - R * 0.88 - 14;
        ly = cy - R * 0.48;
        anchor = 'end';
      }

      const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      txt.setAttribute('class', 'bx-radar-label');
      txt.setAttribute('x', lx.toFixed(1));
      txt.setAttribute('y', ly.toFixed(1));
      txt.setAttribute('text-anchor', anchor);

      const spanName = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
      spanName.setAttribute('class', 'bx-radar-label-name');
      spanName.textContent = mainName;
      txt.appendChild(spanName);

      const spanScore = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
      spanScore.setAttribute('class', `bx-radar-label-score ${scoreClass}`);
      spanScore.textContent = ` ${score}%`;
      txt.appendChild(spanScore);

      labelsLayer.appendChild(txt);
    }

    // 4. 计算数据多边形顶点与交互数据点
    const dataPoints = [];
    dims.forEach((d, i) => {
      const a = i * angleStep - Math.PI / 2;
      const ratio = Math.min(100, Math.max(12, d.score || 50)) / 100;
      const x = cx + R * ratio * Math.cos(a);
      const y = cy + R * ratio * Math.sin(a);
      dataPoints.push(`${x.toFixed(1)},${y.toFixed(1)}`);

      const score = Math.round(d.score || 0);
      const scoreColor = score >= 80 ? '#10b981' : (score >= 60 ? '#0284c7' : '#ea580c');

      const grp = document.createElementNS('http://www.w3.org/2000/svg', 'g');
      grp.setAttribute('class', 'bx-radar-point-group');
      grp.setAttribute('data-label', d.label);
      grp.setAttribute('data-score', score);

      const outerDot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      outerDot.setAttribute('class', 'bx-radar-point-outer');
      outerDot.setAttribute('cx', x.toFixed(1));
      outerDot.setAttribute('cy', y.toFixed(1));
      outerDot.setAttribute('r', '4.5');
      grp.appendChild(outerDot);

      const innerDot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      innerDot.setAttribute('class', 'bx-radar-point-inner');
      innerDot.setAttribute('cx', x.toFixed(1));
      innerDot.setAttribute('cy', y.toFixed(1));
      innerDot.setAttribute('r', '2.5');
      innerDot.style.fill = scoreColor;
      grp.appendChild(innerDot);

      const hitArea = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      hitArea.setAttribute('cx', x.toFixed(1));
      hitArea.setAttribute('cy', y.toFixed(1));
      hitArea.setAttribute('r', '14');
      hitArea.setAttribute('fill', 'transparent');
      hitArea.style.cursor = 'pointer';
      grp.appendChild(hitArea);

      // Tooltip Hover Interactions
      grp.onmouseenter = (e) => {
        if (!tooltip) return;
        const wrapRect = document.getElementById('bx-radar-chart-wrap').getBoundingClientRect();
        const screenX = (x / 340) * wrapRect.width;
        const screenY = (y / 220) * wrapRect.height;
        tooltip.style.left = `${screenX}px`;
        tooltip.style.top = `${screenY}px`;
        tooltip.innerHTML = `
          <div style="font-weight:700; color:#38bdf8; margin-bottom:4px; font-size:0.8rem;">
            ${escapeHtml(d.label)}
          </div>
          <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:4px;">
            <span>防护成熟度:</span>
            <strong style="color:${scoreColor}; font-size:0.85rem;">${score} / 100</strong>
          </div>
          <div style="font-size:0.7rem; color:#94a3b8; border-top:1px solid rgba(255,255,255,0.1); padding-top:4px; margin-top:4px;">
            ${score >= 80 ? '全链路污点追踪已覆盖 · 风险可控' : (score >= 60 ? '具备基础检测策略 · 建议加强深挖' : '暴露面防护薄弱 · 建议优先补充挖掘规则')}
          </div>
        `;
        tooltip.classList.add('show');
      };

      grp.onmouseleave = () => {
        if (tooltip) tooltip.classList.remove('show');
      };

      pointsLayer.appendChild(grp);
    });

    poly.setAttribute('points', dataPoints.join(' '));

    // 5. 渲染雷达下方微型指标看板 (Micro Metrics Bar)
    if (metricsBar) {
      const coveredCnt = dims.filter(d => (d.score || 0) >= 60).length;
      const sorted = [...dims].sort((a, b) => (b.score || 0) - (a.score || 0));
      const maxDim = sorted[0];
      const minDim = sorted[sorted.length - 1];

      metricsBar.innerHTML = `
        <div class="bx-radar-metric-item">
          <span class="bx-radar-metric-label">防线覆盖</span>
          <span class="bx-radar-metric-val good">${coveredCnt} / ${dims.length} 维</span>
        </div>
        <div class="bx-radar-metric-item">
          <span class="bx-radar-metric-label">最高成熟度</span>
          <span class="bx-radar-metric-val good">${maxDim.label.replace(/\s*\(.*?\)/, '').trim()} · ${Math.round(maxDim.score)}%</span>
        </div>
        <div class="bx-radar-metric-item">
          <span class="bx-radar-metric-label">薄弱暴露面</span>
          <span class="bx-radar-metric-val warn">${minDim.label.replace(/\s*\(.*?\)/, '').trim()} · ${Math.round(minDim.score)}%</span>
        </div>
      `;
    }

    // 6. 综合评级徽章
    const healthBadge = document.getElementById('bx-radar-health-badge');
    if (healthBadge) {
      const grade = (strat && strat.health_grade) ? strat.health_grade.toUpperCase() : 'B';
      let gradeCls = 'bx-badge bx-badge-green';
      let gradeText = `Grade ${grade} · 良好`;
      if (grade === 'A') {
        gradeCls = 'bx-badge bx-badge-green';
        gradeText = `Grade A · 极佳`;
      } else if (grade === 'B') {
        gradeCls = 'bx-badge bx-badge-cyan';
        gradeText = `Grade B · 良好`;
      } else if (grade === 'C') {
        gradeCls = 'bx-badge bx-badge-amber';
        gradeText = `Grade C · 需关注`;
      } else {
        gradeCls = 'bx-badge bx-badge-danger';
        gradeText = `Grade ${grade} · 高风险`;
      }
      healthBadge.className = gradeCls;
      healthBadge.innerText = gradeText;
    }
  }

  function renderStrategyFlowDiagram(mining, currentSettings) {
    const wrap = document.getElementById('bx-arch-diagram-wrap');
    if (!wrap) return;

    if (!window._bxArchTheme) {
      window._bxArchTheme = 'paper';
    }
    if (!window._bxArchMode) {
      window._bxArchMode = 'mining';
    }

    wrap.className = `bx-arch-diagram-card theme-${window._bxArchTheme}`;

    // 1. Mode Switch Controls (Bug Mining vs. Strategy Architecture)
    const btnMining = document.getElementById('bx-arch-btn-mining');
    const btnStrategy = document.getElementById('bx-arch-btn-strategy');
    const titleText = document.getElementById('bx-arch-title-text');
    const subText = document.getElementById('bx-arch-sub-text');

    if (btnMining && btnStrategy) {
      btnMining.classList.toggle('active', window._bxArchMode === 'mining');
      btnStrategy.classList.toggle('active', window._bxArchMode === 'strategy');

      btnMining.onclick = () => {
        window._bxArchMode = 'mining';
        renderStrategyFlowDiagram(mining, currentSettings);
      };

      btnStrategy.onclick = () => {
        window._bxArchMode = 'strategy';
        renderStrategyFlowDiagram(mining, currentSettings);
      };
    }

    if (titleText && subText) {
      if (window._bxArchMode === 'mining') {
        titleText.innerHTML = `<span style="display:inline-flex; align-items:center; gap:6px;">${BxIcons.bug(15)} 漏洞深度挖掘流转图 (Bug Mining Flow)</span>`;
        subText.innerText = `从暴露面污点注入、并发变异挖掘、确定性 PoC 复现到根因诊断与补丁合成的端到端闭环`;
      } else {
        titleText.innerHTML = `<span style="display:inline-flex; align-items:center; gap:6px;">${BxIcons.topology(15)} 策略拆解模式流转拓扑架构 (Strategy Flow Architecture)</span>`;
        subText.innerText = `静态勘测 → 运行时自治编排 → 沙箱对抗验证 → 漏洞账本全链路流转拓扑`;
      }
    }

    // 2. Theme Switch Controls (Paper Ivory vs. Cyber Dark)
    const btnPaper = document.getElementById('bx-arch-btn-paper');
    const btnDark = document.getElementById('bx-arch-btn-dark');

    if (btnPaper && btnDark) {
      btnPaper.classList.toggle('active', window._bxArchTheme === 'paper');
      btnDark.classList.toggle('active', window._bxArchTheme === 'dark');

      btnPaper.onclick = () => {
        window._bxArchTheme = 'paper';
        wrap.className = 'bx-arch-diagram-card theme-paper';
        btnPaper.classList.add('active');
        btnDark.classList.remove('active');
      };

      btnDark.onclick = () => {
        window._bxArchTheme = 'dark';
        wrap.className = 'bx-arch-diagram-card theme-dark';
        btnDark.classList.add('active');
        btnPaper.classList.remove('active');
      };
    }

    // Dynamic Data Attributes
    const strat = (mining && mining.strategy) || {};
    const prog = (mining && mining.progress) || {};
    const findings = (mining && mining.findings) || {};
    const pos = (currentSettings && currentSettings.profile && currentSettings.profile.positioning) || {};

    const primaryLang = (pos.primary_language || (strat.languages && strat.languages[0]) || 'Project').toUpperCase();
    const scaleLevel = (pos.scale_level || 'standard').toUpperCase();
    const surfacesCount = (strat.surfaces && strat.surfaces.length) ? strat.surfaces.length : 4;
    const surfacesText = `${surfacesCount} Surfaces Map`;
    const supervisorSub = `Policy: ${strat.polling_policy || 'auto'} · ${strat.recommended_concurrency || 4} Workers`;
    const flawsText = `${findings.total || 0} Flaws (${findings.patches_generated || 0} Patched)`;
    const streamSub = `Phase: ${prog.current_phase || 'Active'} · ${prog.elapsed_display || '0s'}`;
    const patchesText = `${findings.patches_generated || 0} Patches Verified`;
    const concurrencyText = `${strat.recommended_concurrency || 4} Workers · Auto Handoff`;

    if (window._bxArchMode === 'mining') {
      // -----------------------------------------------------------------------
      // MODE A: Bug 漏洞深度挖掘全生命周期流转图 (Bug Mining & Exploit Pipeline)
      // -----------------------------------------------------------------------
      wrap.innerHTML = `
        <svg class="bx-arch-svg" viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arch-arrow-gray" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
              <polygon points="0 0, 7 3.5, 0 7" fill="var(--arch-edge-secondary)" />
            </marker>
            <marker id="arch-arrow-blue" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
              <polygon points="0 0, 7 3.5, 0 7" fill="var(--arch-edge-primary)" />
            </marker>
            <filter id="arch-focal-glow" x="-10%" y="-10%" width="125%" height="130%">
              <feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="var(--arch-focal-border)" flood-opacity="0.28" />
            </filter>
          </defs>

          <!-- 1. Header Typography (ByteByteGo Archetype) -->
          <g class="bx-arch-header-group">
            <text x="36" y="34" fill="var(--arch-eyebrow)" font-size="10.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="1.2">VULNERABILITY MINING &amp; EXPLOIT PIPELINE</text>
            <text x="36" y="64" fill="var(--arch-text-primary)" font-size="23" font-family="'Source Serif 4', Georgia, serif" font-weight="600" letter-spacing="-0.3">Automated Bug Hunting &amp; Patch Synthesis</text>
            <text x="36" y="86" fill="var(--arch-text-secondary)" font-size="12.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">End-to-end pipeline from attack surface probing to hypothesis generation, PoC reproduction, and dual-track patch verification.</text>
          </g>

          <!-- 2. Core Mining Runtime Container Box -->
          <g class="bx-arch-container-group">
            <rect x="404" y="108" width="394" height="264" rx="10" fill="var(--arch-container-bg)" stroke="var(--arch-container-border)" stroke-width="1.2" stroke-dasharray="5 4" />
            <text x="420" y="129" fill="var(--arch-container-label)" font-size="11" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="1">DEEP MINING &amp; SYNTHESIS RUNTIME</text>
          </g>

          <!-- 3. Edges & Connectors Layer -->
          <g class="bx-arch-edges-layer">
            <!-- Flow 1: Target -> Fuzz Hypothesis -->
            <line x1="190" y1="180" x2="224" y2="180" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="192" y="171" width="32" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="208" y="183.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">TAINT</text>

            <!-- Flow 2: Fuzz Hypothesis -> Swarm Workers (Primary Path) -->
            <line x1="378" y1="180" x2="424" y2="180" stroke="var(--arch-edge-primary)" stroke-width="2" marker-end="url(#arch-arrow-blue)" />
            <rect x="382" y="171" width="40" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-primary)" stroke-width="1" />
            <text x="402" y="183.5" fill="var(--arch-edge-primary)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">DISPATCH</text>

            <!-- Flow 3: Swarm Workers -> PoC Reproducer (Vertical) -->
            <line x1="501" y1="215" x2="501" y2="275" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="479" y="236" width="44" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="501" y="248.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">CRASH</text>

            <!-- Flow 4: Swarm Workers -> Root Cause Analyzer (Horizontal) -->
            <line x1="578" y1="180" x2="624" y2="180" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="578" y="171" width="46" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="601" y="183.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">ANOMALY</text>

            <!-- Flow 5: PoC Reproducer -> Patch Synthesizer (Horizontal) -->
            <line x1="578" y1="310" x2="624" y2="310" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="577" y="301" width="48" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="601" y="313.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">POC.FAIL</text>

            <!-- Flow 6: Root Cause Analyzer -> Patch Synthesizer (Vertical) -->
            <line x1="701" y1="215" x2="701" y2="275" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="670" y="236" width="62" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="701" y="248.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">ROOT.CAUSE</text>

            <!-- Flow 7: Patch Synthesizer -> Differential Gate (Horizontal out of container) -->
            <line x1="778" y1="310" x2="824" y2="310" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="776" y="301" width="50" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="801" y="313.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">POC.PASS</text>

            <!-- Flow 8: Root Cause Analyzer -> Differential Gate (Async Curved Path) -->
            <path d="M 778 180 H 889 A 10 10 0 0 1 899 190 V 275" fill="none" stroke="var(--arch-edge-secondary)" stroke-width="1.2" stroke-dasharray="4 3" marker-end="url(#arch-arrow-gray)" />
            <rect x="808" y="171" width="56" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="836" y="183.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">REGRESSION</text>
          </g>

          <!-- 4. Interactive Nodes Layer -->
          <g class="bx-arch-nodes-layer">
            <!-- Node 1: Target Codebase [TARGET] (External) -->
            <g class="bx-arch-node" data-node="target" transform="translate(36, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" stroke-dasharray="4 3" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">TARGET</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Target Codebase</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${primaryLang} · ${surfacesText}</text>
            </g>

            <!-- Node 2: Fuzz Hypothesis [PROBE] (Service) -->
            <g class="bx-arch-node" data-node="hypothesis" transform="translate(224, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">PROBE</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Fuzz Hypothesis</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Mutation &amp; tainted inputs</text>
            </g>

            <!-- Node 3: Swarm Workers [SWARM] (Focal Node) -->
            <g class="bx-arch-node bx-arch-focal" data-node="swarm" transform="translate(424, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-focal-bg)" stroke="var(--arch-focal-border)" stroke-width="2" filter="url(#arch-focal-glow)" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-focal-badge-bg)" stroke="var(--arch-focal-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-focal-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">SWARM</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Swarm Workers</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${concurrencyText}</text>
            </g>

            <!-- Node 4: PoC Reproducer [REPRO] (Store Node) -->
            <g class="bx-arch-node" data-node="repro" transform="translate(424, 275)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">REPRO</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">PoC Reproducer</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${flawsText}</text>
            </g>

            <!-- Node 5: Root Cause Analyzer [DIAG] (Service Node) -->
            <g class="bx-arch-node" data-node="diagnosis" transform="translate(624, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="42" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="31" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">DIAG</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Root Cause Analyzer</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Stack trace &amp; AST slice</text>
            </g>

            <!-- Node 6: Patch Synthesizer [SYNTH] (Service Node) -->
            <g class="bx-arch-node" data-node="synth" transform="translate(624, 275)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">SYNTH</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Patch Synthesizer</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${patchesText}</text>
            </g>

            <!-- Node 7: Differential Gate [GATE] (Service Node) -->
            <g class="bx-arch-node" data-node="gate" transform="translate(824, 275)">
              <rect class="bx-arch-node-box" width="150" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="44" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="32" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">GATE</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Differential Gate</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Dual-blind verification</text>
            </g>
          </g>

          <!-- 5. Legend Layer -->
          <g class="bx-arch-legend-group">
            <line x1="36" y1="412" x2="964" y2="412" stroke="var(--arch-divider)" stroke-width="1" />

            <text x="36" y="434" fill="var(--arch-eyebrow)" font-size="10.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="0.8">Node types</text>
            
            <rect x="36" y="446" width="22" height="14" rx="3" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1" stroke-dasharray="3 2" />
            <text x="66" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Target</text>

            <rect x="130" y="446" width="22" height="14" rx="3" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1" />
            <text x="160" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Service</text>

            <rect x="222" y="446" width="22" height="14" rx="3" fill="var(--arch-focal-bg)" stroke="var(--arch-focal-border)" stroke-width="1.8" />
            <text x="252" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Focal</text>

            <g transform="translate(304, 446)">
              <rect width="22" height="14" rx="3" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1" />
              <line x1="4" y1="5" x2="18" y2="5" stroke="var(--arch-badge-text)" stroke-width="1.2" />
              <line x1="4" y1="9" x2="18" y2="9" stroke="var(--arch-badge-text)" stroke-width="1.2" />
            </g>
            <text x="334" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Repro</text>

            <text x="470" y="434" fill="var(--arch-eyebrow)" font-size="10.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="0.8">Edge types</text>

            <line x1="470" y1="453" x2="498" y2="453" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <text x="508" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Taint / input</text>

            <line x1="600" y1="453" x2="628" y2="453" stroke="var(--arch-edge-primary)" stroke-width="2" marker-end="url(#arch-arrow-blue)" />
            <text x="638" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Primary path</text>

            <line x1="732" y1="453" x2="760" y2="453" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <text x="770" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Diagnosis</text>

            <line x1="846" y1="453" x2="874" y2="453" stroke="var(--arch-edge-secondary)" stroke-width="1.2" stroke-dasharray="3 2" marker-end="url(#arch-arrow-gray)" />
            <text x="884" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Async regression</text>
          </g>
        </svg>
      `;
    } else {
      // -----------------------------------------------------------------------
      // MODE B: 策略拆解与核心运行时架构 (Strategy & Runtime Architecture)
      // -----------------------------------------------------------------------
      wrap.innerHTML = `
        <svg class="bx-arch-svg" viewBox="0 0 1000 500" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arch-arrow-gray" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
              <polygon points="0 0, 7 3.5, 0 7" fill="var(--arch-edge-secondary)" />
            </marker>
            <marker id="arch-arrow-blue" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
              <polygon points="0 0, 7 3.5, 0 7" fill="var(--arch-edge-primary)" />
            </marker>
            <filter id="arch-focal-glow" x="-10%" y="-10%" width="125%" height="130%">
              <feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="var(--arch-focal-border)" flood-opacity="0.28" />
            </filter>
          </defs>

          <!-- 1. Header Typography (ByteByteGo / Stripe Gold-Standard Archetype) -->
          <g class="bx-arch-header-group">
            <text x="36" y="34" fill="var(--arch-eyebrow)" font-size="10.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="1.2">SYSTEM ARCHITECTURE</text>
            <text x="36" y="64" fill="var(--arch-text-primary)" font-size="23" font-family="'Source Serif 4', Georgia, serif" font-weight="600" letter-spacing="-0.3">BugX Strategy &amp; Core Runtime Topology</text>
            <text x="36" y="86" fill="var(--arch-text-secondary)" font-size="12.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">High-level topology of attack surface scouting, supervisor orchestration, and finding ledger pipelines.</text>
          </g>

          <!-- 2. Core Runtime Container Box -->
          <g class="bx-arch-container-group">
            <rect x="404" y="108" width="394" height="264" rx="10" fill="var(--arch-container-bg)" stroke="var(--arch-container-border)" stroke-width="1.2" stroke-dasharray="5 4" />
            <text x="420" y="129" fill="var(--arch-container-label)" font-size="11" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="1">CORE RUNTIME (SUPERVISOR ENGINE)</text>
          </g>

          <!-- 3. Edges & Connectors Layer (Drawn behind nodes) -->
          <g class="bx-arch-edges-layer">
            <!-- Flow 1: Workspace -> Surface Scout -->
            <line x1="190" y1="180" x2="224" y2="180" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="194" y="171" width="28" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="208" y="183.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">AST</text>

            <!-- Flow 2: Surface Scout -> Supervisor Host (Primary Path) -->
            <line x1="378" y1="180" x2="424" y2="180" stroke="var(--arch-edge-primary)" stroke-width="2" marker-end="url(#arch-arrow-blue)" />
            <rect x="385" y="171" width="34" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-primary)" stroke-width="1" />
            <text x="402" y="183.5" fill="var(--arch-edge-primary)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">PLAN</text>

            <!-- Flow 3: Supervisor Host -> Finding Ledger (Vertical) -->
            <line x1="501" y1="215" x2="501" y2="275" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="473" y="236" width="56" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="501" y="248.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">FINDINGS</text>

            <!-- Flow 4: Supervisor Host -> Sandbox Verifier (Horizontal) -->
            <line x1="578" y1="180" x2="624" y2="180" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="585" y="171" width="32" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="601" y="183.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">EXEC</text>

            <!-- Flow 5: Finding Ledger -> Live Event Bus (Horizontal) -->
            <line x1="578" y1="310" x2="624" y2="310" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="583" y="301" width="36" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="601" y="313.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">AUDIT</text>

            <!-- Flow 6: Sandbox Verifier -> Live Event Bus (Vertical) -->
            <line x1="701" y1="215" x2="701" y2="275" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="677" y="236" width="48" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="701" y="248.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">VERIFIED</text>

            <!-- Flow 7: Live Event Bus -> Dual-Track Reports (Horizontal out of container) -->
            <line x1="778" y1="310" x2="824" y2="310" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <rect x="784" y="301" width="34" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="801" y="313.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">EMIT</text>

            <!-- Flow 8: Sandbox Verifier -> Dual-Track Reports (Async Curved Path) -->
            <path d="M 778 180 H 889 A 10 10 0 0 1 899 190 V 275" fill="none" stroke="var(--arch-edge-secondary)" stroke-width="1.2" stroke-dasharray="4 3" marker-end="url(#arch-arrow-gray)" />
            <rect x="811" y="171" width="50" height="18" rx="3" fill="var(--arch-edge-badge-bg)" stroke="var(--arch-edge-badge-border)" stroke-width="1" />
            <text x="836" y="183.5" fill="var(--arch-edge-badge-text)" font-size="8.5" font-family="ui-monospace, monospace" font-weight="600" text-anchor="middle" dominant-baseline="middle">PATCHES</text>
          </g>

          <!-- 4. Interactive Nodes Layer -->
          <g class="bx-arch-nodes-layer">
            <!-- Node 1: Project Workspace [CODE] (External) -->
            <g class="bx-arch-node" data-node="workspace" transform="translate(36, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" stroke-dasharray="4 3" />
              <rect x="10" y="10" width="38" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="29" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">CODE</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Project Workspace</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${primaryLang} · ${scaleLevel}</text>
            </g>

            <!-- Node 2: Surface Scout [INGRESS] (Service) -->
            <g class="bx-arch-node" data-node="scout" transform="translate(224, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="50" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="35" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">INGRESS</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Surface Scout</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${surfacesText}</text>
            </g>

            <!-- Node 3: Supervisor Host [ORCH] (Focal Node) -->
            <g class="bx-arch-node bx-arch-focal" data-node="supervisor" transform="translate(424, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-focal-bg)" stroke="var(--arch-focal-border)" stroke-width="2" filter="url(#arch-focal-glow)" />
              <rect x="10" y="10" width="40" height="15" rx="3" fill="var(--arch-focal-badge-bg)" stroke="var(--arch-focal-border)" stroke-width="0.8" />
              <text x="30" y="18" fill="var(--arch-focal-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">ORCH</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Supervisor Host</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${supervisorSub}</text>
            </g>

            <!-- Node 4: Finding Ledger [STORE] (Store Node) -->
            <g class="bx-arch-node" data-node="ledger" transform="translate(424, 275)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="44" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="32" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">STORE</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Finding Ledger</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${flawsText}</text>
            </g>

            <!-- Node 5: Sandbox Verifier [VERIFY] (Service Node) -->
            <g class="bx-arch-node" data-node="verifier" transform="translate(624, 145)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">VERIFY</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Sandbox Verifier</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Anti-slop test runner</text>
            </g>

            <!-- Node 6: Live Event Bus [STREAM] (Service Node) -->
            <g class="bx-arch-node" data-node="events" transform="translate(624, 275)">
              <rect class="bx-arch-node-box" width="154" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="48" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="34" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">STREAM</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Live Event Bus</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">${streamSub}</text>
            </g>

            <!-- Node 7: Dual-Track Reports [OUTPUT] (Service Node) -->
            <g class="bx-arch-node" data-node="reports" transform="translate(824, 275)">
              <rect class="bx-arch-node-box" width="150" height="70" rx="8" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1.2" />
              <rect x="10" y="10" width="46" height="15" rx="3" fill="var(--arch-badge-bg)" stroke="var(--arch-badge-border)" stroke-width="0.8" />
              <text x="33" y="18" fill="var(--arch-badge-text)" font-size="9" font-family="ui-monospace, monospace" font-weight="700" text-anchor="middle" dominant-baseline="middle">OUTPUT</text>
              <text x="10" y="44" fill="var(--arch-text-primary)" font-size="13" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600">Dual-Track Reports</text>
              <text x="10" y="59" fill="var(--arch-text-secondary)" font-size="10" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Verified diffs &amp; audit</text>
            </g>
          </g>

          <!-- 5. Legend Layer (ByteByteGo Gold-Standard Archetype) -->
          <g class="bx-arch-legend-group">
            <line x1="36" y1="412" x2="964" y2="412" stroke="var(--arch-divider)" stroke-width="1" />

            <text x="36" y="434" fill="var(--arch-eyebrow)" font-size="10.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="0.8">Node types</text>
            
            <rect x="36" y="446" width="22" height="14" rx="3" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1" stroke-dasharray="3 2" />
            <text x="66" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">External</text>

            <rect x="130" y="446" width="22" height="14" rx="3" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1" />
            <text x="160" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Service</text>

            <rect x="222" y="446" width="22" height="14" rx="3" fill="var(--arch-focal-bg)" stroke="var(--arch-focal-border)" stroke-width="1.8" />
            <text x="252" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Focal</text>

            <g transform="translate(304, 446)">
              <rect width="22" height="14" rx="3" fill="var(--arch-card-bg)" stroke="var(--arch-card-border)" stroke-width="1" />
              <line x1="4" y1="5" x2="18" y2="5" stroke="var(--arch-badge-text)" stroke-width="1.2" />
              <line x1="4" y1="9" x2="18" y2="9" stroke="var(--arch-badge-text)" stroke-width="1.2" />
            </g>
            <text x="334" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Store</text>

            <text x="470" y="434" fill="var(--arch-eyebrow)" font-size="10.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif" font-weight="600" letter-spacing="0.8">Edge types</text>

            <line x1="470" y1="453" x2="498" y2="453" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <text x="508" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">HTTP / API</text>

            <line x1="596" y1="453" x2="624" y2="453" stroke="var(--arch-edge-primary)" stroke-width="2" marker-end="url(#arch-arrow-blue)" />
            <text x="634" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Primary path</text>

            <line x1="732" y1="453" x2="760" y2="453" stroke="var(--arch-edge-secondary)" stroke-width="1.2" marker-end="url(#arch-arrow-gray)" />
            <text x="770" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Call / flow</text>

            <line x1="854" y1="453" x2="882" y2="453" stroke="var(--arch-edge-secondary)" stroke-width="1.2" stroke-dasharray="3 2" marker-end="url(#arch-arrow-gray)" />
            <text x="892" y="457" fill="var(--arch-text-primary)" font-size="11.5" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif">Async</text>
          </g>
        </svg>
      `;
    }
  }

  function renderMiningTab(mining) {
    const prog = mining.progress || {};
    const strat = mining.strategy || {};
    const rounds = mining.rounds_history || [];
    const findings = mining.findings || {};
    const chartData = mining.chart_data || {};

    // 0. Update LIVE Monitoring Header info
    const liveRoundInfo = document.getElementById('bx-live-round-info');
    if (liveRoundInfo) {
      liveRoundInfo.innerText = mining.current_polling_id 
        ? `活跃轮次: #${mining.current_polling_no || 1} (${mining.current_polling_id})` 
        : '就绪待命 · 随时开启全息挖掘';
    }

    // 1. Progress Status Badge
    const statusBadge = document.getElementById('bx-mining-status-badge');
    if (statusBadge) {
      const st = (prog.status || 'idle').toLowerCase();
      if (st === 'running' || st === 'active') {
        statusBadge.innerText = '运行中 (RUNNING)';
        statusBadge.className = 'bx-badge bx-badge-green';
      } else if (st === 'completed') {
        statusBadge.innerText = '本轮已收敛 (COMPLETED)';
        statusBadge.className = 'bx-badge bx-badge-cyan';
      } else if (st === 'budget_exhausted') {
        statusBadge.innerText = '预算已封板 (BUDGET)';
        statusBadge.className = 'bx-badge bx-badge-amber';
      } else if (st === 'aborted') {
        statusBadge.innerText = '异常中断 (ABORTED)';
        statusBadge.className = 'bx-badge bx-badge-gray';
      } else {
        statusBadge.innerText = '就绪待命 (IDLE)';
        statusBadge.className = 'bx-badge bx-badge-gray';
      }
    }

    // 2. Flowing Pipeline Topology (Animated)
    renderFlowingPipeline(prog);

    // 3. Radial Coverage Gauge & Progress Bar
    renderRadialGauge(prog.coverage_pct || 0);

    const covDetail = document.getElementById('bx-coverage-detail');
    const covBar = document.getElementById('bx-coverage-bar');
    if (covDetail) {
      covDetail.innerText = `${prog.coverage_covered || 0} / ${prog.coverage_total || 0} (${prog.coverage_pct || 0}%)`;
    }
    if (covBar) {
      covBar.style.width = Math.min(100, Math.max(0, prog.coverage_pct || 0)) + '%';
    }

    // 3.1 Render Strategy-Group Waffle Coverage Matrix (REQ-07)
    renderWaffleGrid(prog);

    // Progress metrics
    const elapsedEl = document.getElementById('bx-mining-elapsed');
    if (elapsedEl) elapsedEl.innerText = prog.elapsed_display || '0s';
    const toolsEl = document.getElementById('bx-mining-tools');
    if (toolsEl) toolsEl.innerText = (prog.tool_calls || 0) + ' 次';
    const tokensEl = document.getElementById('bx-mining-tokens');
    if (tokensEl) tokensEl.innerText = Number(prog.tokens_consumed || 0).toLocaleString();
    const phaseEl = document.getElementById('bx-mining-current-phase');
    if (phaseEl) phaseEl.innerText = prog.current_phase || '就绪';

    // 4. Render Dual Dynamic Charts (Trend Chart & Radar Chart)
    renderRoundTrendChart(chartData, rounds);
    renderRadarChart(chartData, strat);

    // 5. Strategy & Profiles
    const stratPolling = document.getElementById('bx-strat-polling');
    if (stratPolling) stratPolling.innerText = strat.polling_policy === 'auto' ? '持续自治轮询 (auto)' : (strat.polling_policy === 'manual' ? '单轮手动触发 (manual)' : '关闭轮询');
    const stratReview = document.getElementById('bx-strat-review');
    if (stratReview) stratReview.innerText = strat.review_mode === 'auto' ? '全自动深入审查 (auto)' : (strat.review_mode === 'interactive' ? '交互式逐步确认' : '关闭审查');
    const stratScale = document.getElementById('bx-strat-scale');
    if (stratScale) stratScale.innerText = `推荐 ${strat.recommended_concurrency || 4} 并发 / ${strat.recommended_rounds || 5} 轮次`;
    const gradeBadge = document.getElementById('bx-strategy-grade-badge');
    if (gradeBadge) gradeBadge.innerText = `安全评级: Grade ${strat.health_grade || 'B'}`;

    // Tags
    const langBox = document.getElementById('bx-strat-languages');
    if (langBox) {
      const langs = strat.languages || [];
      langBox.innerHTML = langs.length ? langs.map(l => `<span class="bx-tag bx-tag-green">${l}</span>`).join('') : '<span class="bx-tag">通用技术栈</span>';
    }
    const surfBox = document.getElementById('bx-strat-surfaces');
    if (surfBox) {
      const surfs = strat.surfaces || [];
      surfBox.innerHTML = surfs.length ? surfs.map(s => `<span class="bx-tag bx-tag-accent">${s}</span>`).join('') : '<span class="bx-tag">全暴露面覆盖</span>';
    }
    const profBox = document.getElementById('bx-strat-profiles');
    if (profBox) {
      const profs = strat.check_profiles || [];
      profBox.innerHTML = profs.length ? profs.map(p => `<span class="bx-tag bx-tag-amber">${p}</span>`).join('') : '<span class="bx-tag">标准攻防规则</span>';
    }

    // 5.1 Mode Flow Architecture Diagram
    renderStrategyFlowDiagram(mining, currentSettings);

    // 6. Rounds Timeline Table
    const curRoundBadge = document.getElementById('bx-current-round-badge');
    if (curRoundBadge) {
      curRoundBadge.innerText = mining.current_polling_id ? `当前活跃轮次: ${mining.current_polling_id} (第 ${mining.current_polling_no || 1} 轮)` : '尚未开展挖掘轮次';
    }

    document.getElementById('bx-num-pollings').innerText = mining.pollings_count || 0;
    document.getElementById('bx-num-sessions').innerText = mining.sessions_count || 0;
    document.getElementById('bx-num-lessons').innerText = mining.lessons_learned || 0;
    document.getElementById('bx-num-patches').innerText = findings.patches_generated || 0;

    const findingsText = document.getElementById('bx-findings-text');
    if (findingsText) {
      findingsText.innerHTML = `累计审计发现漏洞: <strong>${findings.total || 0}</strong> 个 (严重: <span style="color:#ef4444">${findings.critical || 0}</span>, 高危: <span style="color:#f97316">${findings.high || 0}</span>, 中危: <span style="color:#f59e0b">${findings.medium || 0}</span>, 低危: <span style="color:#10b981">${findings.low || 0}</span>)`;
    }

    const roundBody = document.getElementById('bx-rounds-table-body');
    if (roundBody) {
      if (rounds.length === 0) {
        roundBody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--bx-text-muted); padding: 16px;">尚未执行任何挖掘轮次，可输入 <code>/audit</code> 开启第一轮全息勘测</td></tr>`;
      } else {
        roundBody.innerHTML = rounds.map(r => {
          let stColor = 'bx-badge-gray';
          const rSt = (r.state || '').toLowerCase();
          if (rSt === 'running') { stColor = 'bx-badge-green'; }
          else if (rSt === 'completed') { stColor = 'bx-badge-cyan'; }
          else if (rSt === 'aborted') { stColor = 'bx-badge-amber'; }

          return `
            <tr>
              <td><strong style="color: var(--bx-accent-cyan);">#${r.polling_no}</strong></td>
              <td style="font-family: monospace; color: var(--bx-text-main);">${r.polling_id}</td>
              <td><span class="bx-badge ${stColor}">${r.state}</span></td>
              <td style="color: var(--bx-text-muted); font-size: 0.78rem;">${r.created_at || '未知'}</td>
              <td style="color: var(--bx-text-main); font-weight: 500;">${r.elapsed_display}</td>
              <td><strong style="color: ${r.findings_count > 0 ? '#f97316' : 'var(--bx-text-muted)'};">${r.findings_count} 个</strong></td>
            </tr>
          `;
        }).join('');
      }
    }
  }

  function renderProfileTab(profile) {
    if (!profile) return;
    const pos = profile.positioning || {};
    const met = profile.metrics || {};
    const econ = profile.engineering_economics || {};
    const qnr = profile.quality_and_risk || {};
    const langs = profile.languages || [];

    // 1. Positioning
    const primaryLangEl = document.getElementById('bx-prof-primary-lang');
    if (primaryLangEl) primaryLangEl.innerText = (pos.primary_language || 'unknown').toUpperCase();
    const scaleLvlEl = document.getElementById('bx-prof-scale-level');
    if (scaleLvlEl) scaleLvlEl.innerText = (pos.scale_level || 'medium').toUpperCase();
    const catEl = document.getElementById('bx-prof-category');
    if (catEl) catEl.innerText = Array.isArray(pos.category) ? pos.category.join(', ') : (pos.category || 'software-system');

    const scaleBadge = document.getElementById('bx-prof-scale-badge');
    if (scaleBadge) scaleBadge.innerText = `工程体量: ${(pos.scale_level || 'medium').toUpperCase()}`;

    const formsBox = document.getElementById('bx-prof-forms');
    if (formsBox) {
      const forms = pos.forms || [];
      formsBox.innerHTML = forms.length ? forms.map(f => `<span class="bx-prof-pill tag-forms">${BxIcons.box(12)}<span>${escapeHtml(f.toUpperCase())}</span></span>`).join('') : `<span class="bx-prof-pill tag-neutral">${BxIcons.box(12)}<span>通用工程</span></span>`;
    }

    const surfBox = document.getElementById('bx-prof-surfaces');
    if (surfBox) {
      const surfs = pos.surfaces || [];
      surfBox.innerHTML = surfs.length ? surfs.map(s => `<span class="bx-prof-pill tag-surface">${BxIcons.globe(12)}<span>${escapeHtml(s)}</span></span>`).join('') : `<span class="bx-prof-pill tag-neutral">${BxIcons.globe(12)}<span>标准暴露面</span></span>`;
    }

    // 2. Metrics
    const slocEl = document.getElementById('bx-metric-sloc');
    if (slocEl) slocEl.innerText = Number(met.code_lines || 0).toLocaleString();
    const filesEl = document.getElementById('bx-metric-files');
    if (filesEl) filesEl.innerText = Number(met.total_files || 0).toLocaleString();
    const totLinesEl = document.getElementById('bx-metric-total-lines');
    if (totLinesEl) totLinesEl.innerText = Number(met.total_lines || 0).toLocaleString();
    const testRatioEl = document.getElementById('bx-metric-test-ratio');
    if (testRatioEl) {
      const r = (met.test_to_code_ratio || 0) * 100;
      testRatioEl.innerText = r.toFixed(1) + '%';
    }

    // Language Stacked Progress Bar & Tags
    const langBar = document.getElementById('bx-prof-lang-bar');
    if (langBar) {
      if (langs.length > 0) {
        langBar.style.display = 'flex';
        langBar.innerHTML = langs.map(l => {
          const name = (l.language || l.name || 'other').toLowerCase();
          const color = LANG_COLORS[name] || '#8b5cf6';
          const pct = l.percentage !== undefined ? l.percentage.toFixed(1) : '0';
          return `<div class="bx-prof-lang-bar-segment" style="width: ${pct}%; background-color: ${color};" title="${escapeHtml(l.language || l.name || '')}: ${pct}% (${Number(l.lines || l.sloc || 0).toLocaleString()} 行)"></div>`;
        }).join('');
      } else {
        langBar.style.display = 'none';
      }
    }

    const langBox = document.getElementById('bx-prof-languages');
    if (langBox) {
      if (langs.length === 0) {
        langBox.innerHTML = `<span class="bx-prof-pill tag-neutral">${BxIcons.pieChart(12)}<span>未统计多语言</span></span>`;
      } else {
        langBox.innerHTML = langs.map(l => {
          const name = l.language || l.name || '未知';
          const nameLower = name.toLowerCase();
          const color = LANG_COLORS[nameLower] || '#8b5cf6';
          const lines = l.lines || l.sloc || 0;
          const pct = l.percentage !== undefined ? l.percentage.toFixed(1) + '%' : '';
          return `
            <span class="bx-prof-pill tag-lang">
              <span class="bx-lang-dot" style="background-color: ${color};"></span>
              <strong>${escapeHtml(name)}</strong>
              <span style="color: var(--bx-text-muted); margin-left: 2px;">${Number(lines).toLocaleString()} 行</span>
              <span class="bx-prof-lang-pct">${pct}</span>
            </span>
          `;
        }).join('');
      }
    }

    // 3. Engineering Economics
    const pmEl = document.getElementById('bx-econ-pm');
    if (pmEl) pmEl.innerText = `${econ.effort_person_months || 0} PM`;
    const monthsEl = document.getElementById('bx-econ-months');
    if (monthsEl) monthsEl.innerText = `${econ.estimated_schedule_months || 0} 月`;
    const debtDaysEl = document.getElementById('bx-econ-debt-days');
    if (debtDaysEl) debtDaysEl.innerText = `${econ.tech_debt_days || 0} 天`;
    const debtRatioEl = document.getElementById('bx-econ-debt-ratio');
    if (debtRatioEl) debtRatioEl.innerText = `${econ.tech_debt_ratio_pct || 0}%`;

    // 4. Quality & Risk Profile
    const qBadge = document.getElementById('bx-quality-grade-badge');
    if (qBadge) {
      const g = qnr.health_grade || 'B';
      qBadge.innerText = `质量评级: Grade ${g}`;
      qBadge.className = 'bx-badge ' + (g === 'A' ? 'bx-badge-green' : (g === 'B' ? 'bx-badge-cyan' : (g === 'C' ? 'bx-badge-amber' : 'bx-badge-gray')));
    }

    const maintEl = document.getElementById('bx-risk-maintainability');
    if (maintEl) maintEl.innerText = `${qnr.maintainability_index || 0} / 100`;
    const cycEl = document.getElementById('bx-risk-cyclomatic');
    if (cycEl) cycEl.innerText = `${qnr.avg_cyclomatic_complexity || 0}`;
    const dupEl = document.getElementById('bx-risk-duplication');
    if (dupEl) dupEl.innerText = `${qnr.duplication_percentage || 0}%`;
    const sinksEl = document.getElementById('bx-risk-sinks');
    if (sinksEl) sinksEl.innerText = `${qnr.sensitive_sinks || 0} 处`;
    const taintsEl = document.getElementById('bx-risk-taints');
    if (taintsEl) taintsEl.innerText = `${qnr.taint_flows || 0} 条`;
    const secretsEl = document.getElementById('bx-risk-secrets');
    if (secretsEl) secretsEl.innerText = `${qnr.hardcoded_secrets || 0} 处`;
    const godsEl = document.getElementById('bx-risk-gods');
    if (godsEl) godsEl.innerText = `${qnr.god_modules_count || 0} 个`;

    // Top Hotspots Table
    const hsBody = document.getElementById('bx-hotspots-table-body');
    if (hsBody) {
      const hotspots = qnr.top_hotspots || [];
      if (hotspots.length === 0) {
        hsBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--bx-text-muted); padding: 18px 0;"><span style="display: inline-flex; align-items: center; gap: 6px;">${BxIcons.shieldCheck(15)} 暂无高危代码热点</span></td></tr>`;
      } else {
        hsBody.innerHTML = hotspots.map(h => {
          const tags = h.tags || [];
          const tagHtml = tags.map(t => `<span class="bx-tag bx-tag-amber" style="font-size: 0.7rem; font-family: var(--app-monospace-font-family);">${escapeHtml(t)}</span>`).join(' ');
          const score = h.risk_score || 0;
          const riskCls = score >= 80 ? 'bx-badge-red' : (score >= 60 ? 'bx-badge-amber' : 'bx-badge-cyan');
          return `
            <tr>
              <td style="font-family: var(--app-monospace-font-family); font-size: 0.76rem; color: var(--bx-text-main);">
                <span style="display: inline-flex; align-items: center; gap: 6px;">
                  <span style="color: var(--bx-text-muted);">${BxIcons.fileCode(13)}</span>
                  <span>${escapeHtml(h.file || '')}</span>
                </span>
              </td>
              <td><span class="bx-badge ${riskCls}">${score} 分</span></td>
              <td style="color: var(--bx-text-main); font-weight: 500; font-family: var(--app-monospace-font-family);">${Number(h.sloc || 0).toLocaleString()}</td>
              <td>${tagHtml || '<span style="color: var(--bx-text-muted); font-size: 0.72rem;">-</span>'}</td>
            </tr>
          `;
        }).join('');
      }
    }
  }
  window.renderProfileTab = renderProfileTab;

  function renderWaffleGrid(prog) {
    const grid = document.getElementById('bx-waffle-grid');
    const drawer = document.getElementById('bx-waffle-drawer');
    const countBadge = document.getElementById('bx-waffle-count-badge');
    if (!grid) return;

    const groups = prog.strategy_groups || [];
    const total = prog.strategy_groups_total || groups.length;
    const done = prog.strategy_groups_done !== undefined
      ? prog.strategy_groups_done
      : groups.filter(g => g.state && g.state.startsWith('covered_')).length;

    if (countBadge) {
      countBadge.innerText = `${done} / ${total} 策略组 (${prog.coverage_pct || 0}%)`;
    }

    if (groups.length === 0) {
      grid.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: var(--bx-text-muted); font-size: 0.78rem; padding: 12px;">暂无挖掘策略分组数据</div>`;
      return;
    }

    grid.innerHTML = groups.map((g, idx) => {
      const st = g.state || 'pending';
      const label = (g.findings_count > 0) ? `!${g.findings_count}` : (idx + 1);
      const covUnits = g.covered_files !== undefined ? g.covered_files : (g.covered_units || 0);
      const totUnits = g.file_count !== undefined ? g.file_count : (g.total_units || 1);
      const tip = `[#${idx + 1}] ${g.title || g.group_id}\n状态: ${st}\n覆盖单元: ${covUnits}/${totUnits}\n发现漏洞: ${g.findings_count || 0} 个`;
      return `<div class="bx-waffle-cell state-${st}" data-group-idx="${idx}" title="${tip.replace(/"/g, '&quot;')}">${label}</div>`;
    }).join('');

    grid.querySelectorAll('.bx-waffle-cell').forEach(cell => {
      cell.onclick = () => {
        const idx = parseInt(cell.getAttribute('data-group-idx'), 10);
        const grp = groups[idx];
        if (!grp || !drawer) return;
        const files = grp.files || [];
        const firstFile = files[0] || '';
        const covUnits = grp.covered_files !== undefined ? grp.covered_files : (grp.covered_units || 0);
        const totUnits = grp.file_count !== undefined ? grp.file_count : (grp.total_units || 1);
        drawer.classList.add('active');
        drawer.innerHTML = `
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div>
              <strong style="color: var(--bx-accent-cyan); font-size: 0.88rem; display: inline-flex; align-items: center; gap: 6px;">${BxIcons.package(14)} 策略分组 #${idx + 1}: ${grp.title || grp.group_id}</strong>
              <span class="bx-badge bx-badge-green" style="margin-left: 8px;">状态: ${grp.state}</span>
              <span class="bx-badge bx-badge-amber" style="margin-left: 4px;">漏洞: ${grp.findings_count || 0}</span>
            </div>
            <div style="display: flex; gap: 6px;">
              ${firstFile ? `<button type="button" class="bx-action-btn primary" onclick="window.openFileInIDE('${firstFile.replace(/'/g, "\\'")}', 1)"><span>${BxIcons.launch(13)}</span> <span>在 IDE 打开源码</span></button>` : ''}
              <button type="button" class="bx-action-btn" onclick="window.insertSkillToChat('/mining 针对策略组 ${grp.group_id} (${(grp.title || '').replace(/'/g, '')}) 执行定向深度漏洞挖掘')"><span>${BxIcons.target(13)}</span> <span>定向挖掘此分组</span></button>
              <button type="button" class="bx-action-btn" onclick="document.getElementById('bx-waffle-drawer').classList.remove('active')">关闭</button>
            </div>
          </div>
          <div style="font-size: 0.76rem; color: var(--bx-text-muted);">
            覆盖单元: <strong>${covUnits} / ${totUnits}</strong> · 包含目标文件 (${files.length}):
            <span style="font-family: monospace; color: var(--bx-text-main);">${files.slice(0, 6).join(', ') || '全量范围'}</span>
          </div>
        `;
      };
    });
  }

  async function loadProjectSettings(projHash = '') {
    createModalDOM();
    const endpoint = projHash ? `/projects/${projHash}/settings` : `/project/settings`;
    try {
      const res = await fetch(endpoint);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      currentSettings = data;
      currentHash = data.project_hash || projHash;

      // Update Header
      document.getElementById('bx-proj-path').innerText = data.working_dir || '';
      const sel = document.getElementById('bx-proj-selector');
      if (sel && data.project_hash) {
        sel.value = data.project_hash;
      }
      // Update Archive button label
      const curProj = projectsList.find(p => p.hash === currentHash);
      const isArchived = Boolean(data.archived !== undefined ? data.archived : (curProj && curProj.archived));
      const archBtnTxt = document.getElementById('bx-archive-btn-text');
      if (archBtnTxt) {
        archBtnTxt.innerText = isArchived ? '取消归档' : '归档';
      }

      // 1. Render 5 Gates
      const gates = data.gates || {};
      const fixModeSelect = document.getElementById('bx-select-fix-mode');
      if (fixModeSelect && gates.fix_mode) fixModeSelect.value = gates.fix_mode;

      const cloudPolicySelect = document.getElementById('bx-select-cloud-policy');
      if (cloudPolicySelect && gates.cloud_policy) cloudPolicySelect.value = gates.cloud_policy;

      const reviewSelect = document.getElementById('bx-select-review');
      if (reviewSelect && gates.review_mode) reviewSelect.value = gates.review_mode;

      const pollingSelect = document.getElementById('bx-select-polling');
      if (pollingSelect && gates.polling_policy) pollingSelect.value = gates.polling_policy;

      const commitSelect = document.getElementById('bx-select-commit');
      if (commitSelect && gates.commit_policy) commitSelect.value = gates.commit_policy;

      // 1.1 Render Model & Runtime Config (REQ-03)
      const mc = data.model_config || {};
      const hostSel = document.getElementById('bx-mc-default-host');
      if (hostSel && mc.default_host) hostSel.value = mc.default_host;
      const modelInp = document.getElementById('bx-mc-default-model');
      if (modelInp && mc.default_model !== undefined) modelInp.value = mc.default_model;
      const tempSl = document.getElementById('bx-mc-temperature');
      const tempVl = document.getElementById('bx-mc-temp-val');
      if (tempSl && mc.temperature !== undefined) {
        tempSl.value = mc.temperature;
        if (tempVl) tempVl.innerText = Number(mc.temperature).toFixed(2);
      }
      const topPSl = document.getElementById('bx-mc-top-p');
      const topPVl = document.getElementById('bx-mc-topp-val');
      if (topPSl && mc.top_p !== undefined) {
        topPSl.value = mc.top_p;
        if (topPVl) topPVl.innerText = Number(mc.top_p).toFixed(2);
      }
      const reasSel = document.getElementById('bx-mc-reasoning');
      if (reasSel && mc.reasoning_effort) reasSel.value = mc.reasoning_effort;
      const concSl = document.getElementById('bx-mc-concurrency');
      const concVl = document.getElementById('bx-mc-conc-val');
      if (concSl && mc.worker_concurrency !== undefined) {
        concSl.value = mc.worker_concurrency;
        if (concVl) concVl.innerText = `${mc.worker_concurrency} Workers`;
      }
      const maxTokInp = document.getElementById('bx-mc-max-tokens');
      if (maxTokInp && mc.max_tokens !== undefined) maxTokInp.value = mc.max_tokens;
      const highRiskCb = document.getElementById('bx-mc-high-risk-approval');
      if (highRiskCb) highRiskCb.checked = Boolean(mc.high_risk_write_approval);
      const autoMountCb = document.getElementById('bx-mc-auto-mount');
      if (autoMountCb) autoMountCb.checked = mc.auto_mount_recommended_capabilities !== false;

      // 统一根据最新选值矫正 Switch 开关、徽标与状态文本
      updateGateBadges(false);
      const statusText = document.getElementById('bx-save-status-text');
      if (statusText) {
        statusText.innerText = '✓ 当前配置已与磁盘保持一致';
        statusText.style.color = 'var(--bx-text-muted)';
      }

      // 2. Render Mining (Strategy, Progress & Rounds)
      renderMiningTab(data.mining || {});

      // 3. Render Profile (Positioning & Attributes)
      renderProfileTab(data.profile || {});

      // 4. Render MCPs
      const mcps = data.mcps || {};
      document.getElementById('bx-mcps-count').innerText = mcps.total_servers || 0;
      const mcpBody = document.getElementById('bx-mcp-table-body');
      if (mcpBody) {
        const servers = mcps.servers || [];
        if (servers.length === 0) {
          mcpBody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--bx-text-muted);">当前项目未配置专属 MCP</td></tr>`;
        } else {
          mcpBody.innerHTML = servers.map(s => `
            <tr>
              <td>
                <div style="font-weight: 600; color: var(--bx-text-main);">${s.name}</div>
                <div style="font-size: 0.75rem; color: var(--bx-text-muted); font-family: monospace;">${s.command || ''}</div>
              </td>
              <td><span class="bx-badge bx-badge-cyan">${s.tools || s.tool_count || 0} 个工具</span></td>
              <td><span style="font-size: 0.8rem; color: var(--bx-text-muted);">${s.source || 'default'}</span></td>
              <td><span class="bx-badge bx-badge-green">● ${s.status || 'connected'}</span></td>
              <td>
                <div style="display: flex; gap: 6px;">
                  <button type="button" class="bx-action-btn" onclick="window.promptConfigureMcp('${s.name}')"><span>${BxIcons.key(13)}</span> <span>凭据沙箱</span></button>
                  <button type="button" class="bx-action-btn danger" onclick="window.toggleMcpServer('${s.name}', false)">卸载</button>
                </div>
              </td>
            </tr>
          `).join('');
        }
      }

      // 5. Render Skills
      const skills = data.skills || {};
      document.getElementById('bx-skills-count').innerText = skills.total_skills || 0;
      const skillBody = document.getElementById('bx-skill-table-body');
      if (skillBody) {
        const skList = skills.skills || [];
        skillBody.innerHTML = skList.map(sk => `
          <tr>
            <td>
              <span class="bx-skill-cmd" onclick="window.insertSkillToChat('${sk.command}')" title="点击填入输入框">${sk.command}</span>
            </td>
            <td>
              <span class="bx-badge ${sk.type === 'core' ? 'bx-badge-green' : 'bx-badge-amber'}">${sk.type === 'core' ? '核心审计' : '项目挂载'}</span>
            </td>
            <td style="color: var(--bx-text-main); font-size: 0.84rem;">
              ${sk.description || ''}
            </td>
            <td>
              <div style="display: flex; gap: 6px;">
                <button type="button" class="bx-action-btn" onclick="window.previewSkillMd('${sk.name}')"><span>${BxIcons.file(13)}</span> <span>预览 SKILL.md</span></button>
                <button type="button" class="bx-action-btn" onclick="window.toggleSkillItem('${sk.name}', ${sk.enabled === false ? 'true' : 'false'})">${sk.enabled === false ? '启用' : '停用'}</button>
              </div>
            </td>
          </tr>
        `).join('');
      }

      // 6. Render Project Sessions (REQ-02)
      loadProjectSessions();

    } catch (err) {
      showToast('加载项目设置失败: ' + err.message, true);
    }
  }

  async function loadProjectSessions() {
    const tbody = document.getElementById('bx-sessions-table-body');
    if (!tbody || !currentHash) return;
    try {
      const res = await fetch(`/projects/${encodeURIComponent(currentHash)}/sessions`);
      if (!res.ok) return;
      const data = await res.json();
      const sessions = Array.isArray(data) ? data : (data.sessions || []);
      if (sessions.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--bx-text-muted); padding: 12px;">当前项目暂无历史对话或挖掘会话</td></tr>`;
        return;
      }
      tbody.innerHTML = sessions.map(s => {
        const sid = s.id || s.session_id || '';
        const st = s.status || 'completed';
        const badgeCls = st === 'running' ? 'bx-badge-amber' : (st === 'interrupted' ? 'bx-badge-red' : 'bx-badge-green');
        const dispName = s.name || s.title || sid;
        const safeTitle = dispName.replace(/'/g, "\\'").replace(/"/g, '&quot;');
        const hostName = s.provider || s.host || 'cline';
        const tsVal = s.updated_at || s.created_at || '';
        const tsStr = typeof tsVal === 'number' ? new Date(tsVal).toLocaleString() : String(tsVal);
        return `
          <tr data-session-id="${sid}">
            <td>
              <div style="font-weight: 600; color: var(--bx-text-main);" class="bx-sess-title-text">${dispName}</div>
              <div style="font-size: 0.72rem; font-family: monospace; color: var(--bx-text-muted);">${sid}</div>
            </td>
            <td><span class="bx-tag bx-tag-accent">${hostName}</span></td>
            <td><span class="bx-badge ${badgeCls}">${st}</span></td>
            <td style="font-size: 0.75rem; color: var(--bx-text-muted);">${tsStr}</td>
            <td>
              <div style="display: flex; gap: 6px;">
                <button type="button" class="bx-action-btn primary bx-sess-resume-btn" onclick="window.resumeProjectSession('${sid}', '${hostName}')"><span>${BxIcons.chat(13)}</span> <span>恢复对话</span></button>
                <button type="button" class="bx-action-btn bx-sess-rename-btn" onclick="window.renameProjectSession('${sid}', '${safeTitle}')"><span>${BxIcons.edit(13)}</span> <span>重命名</span></button>
                <button type="button" class="bx-action-btn danger bx-sess-delete-btn" onclick="window.deleteProjectSession('${sid}')"><span>${BxIcons.trash(13)}</span> <span>删除</span></button>
              </div>
            </td>
          </tr>
        `;
      }).join('');
    } catch (e) {
      console.warn('[Sessions]', e);
    }
  }

  window.resumeProjectSession = async function (sessionId, sessionHost) {
    if (!currentHash || !sessionId) return;
    try {
      const res = await fetch(`/projects/${encodeURIComponent(currentHash)}/sessions/${encodeURIComponent(sessionId)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || '加载会话失败');
      window._bxActiveResumeSessionId = sessionId;
      showToast(`已加载并恢复会话 "${data.name || data.title || sessionId}" (宿主: ${sessionHost} · 支持原生续跑或跨宿主历史注入)`);
    } catch (err) {
      showToast('恢复会话失败: ' + err.message, true);
    }
  };

  window.renameProjectSession = async function (sessionId, currentTitle = '') {
    if (!currentHash || !sessionId) return;
    const nextTitle = window._bxMockRenameTitle || window.prompt('请输入新的会话标题:', currentTitle);
    if (!nextTitle || !nextTitle.trim()) return;
    try {
      const res = await fetch(`/projects/${encodeURIComponent(currentHash)}/sessions/${encodeURIComponent(sessionId)}/rename`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: nextTitle.trim(), title: nextTitle.trim() }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '重命名失败');
      showToast(`会话已重命名为: ${data.name || data.title}`);
      await loadProjectSessions();
    } catch (err) {
      showToast('重命名失败: ' + err.message, true);
    }
  };

  window.deleteProjectSession = async function (sessionId) {
    if (!currentHash || !sessionId) return;
    try {
      const res = await fetch(`/projects/${encodeURIComponent(currentHash)}/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE',
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '删除失败');
      showToast(`已删除会话: ${sessionId}`);
      await loadProjectSessions();
    } catch (err) {
      showToast('删除会话失败: ' + err.message, true);
    }
  };

  async function saveAllGates() {
    const payload = {
      fix_mode: document.getElementById('bx-select-fix-mode').value,
      auto_fix_code: document.getElementById('bx-toggle-autofix').checked,
      cloud_policy: document.getElementById('bx-select-cloud-policy').value,
      cloud_sync_enabled: document.getElementById('bx-toggle-cloudsync').checked,
      review_mode: document.getElementById('bx-select-review').value,
      polling_policy: document.getElementById('bx-select-polling').value,
      commit_policy: document.getElementById('bx-select-commit').value,
      model_config: {
        default_host: document.getElementById('bx-mc-default-host')?.value || 'cline',
        default_model: document.getElementById('bx-mc-default-model')?.value || 'auto',
        temperature: parseFloat(document.getElementById('bx-mc-temperature')?.value || '0.2'),
        top_p: parseFloat(document.getElementById('bx-mc-top-p')?.value || '0.95'),
        reasoning_effort: document.getElementById('bx-mc-reasoning')?.value || 'high',
        worker_concurrency: parseInt(document.getElementById('bx-mc-concurrency')?.value || '4', 10),
        max_tokens: parseInt(document.getElementById('bx-mc-max-tokens')?.value || '16384', 10),
        high_risk_write_approval: Boolean(document.getElementById('bx-mc-high-risk-approval')?.checked),
        auto_mount_recommended_capabilities: Boolean(document.getElementById('bx-mc-auto-mount')?.checked),
      },
    };

    const statusText = document.getElementById('bx-save-status-text');
    if (statusText) {
      statusText.innerText = '正在持久化至 .bugx/config.json ...';
      statusText.style.color = 'var(--bx-accent-cyan)';
    }

    const postEndpoint = currentHash ? `/projects/${currentHash}/settings` : `/project/settings`;

    try {
      const res = await fetch(postEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const errJson = await res.json().catch(() => ({}));
        throw new Error(errJson.error || ('HTTP ' + res.status));
      }
      const data = await res.json();
      currentSettings = data;
      showToast('✓ 五大门禁与模型运行时配置已保存 (下个子任务平滑热生效)！');
      updateGateBadges(false);
      if (statusText) {
        statusText.innerText = '✓ 配置已持久化生效 (Next-Task Hot Reload)';
        statusText.style.color = 'var(--bx-accent-green)';
      }
    } catch (err) {
      showToast('✗ 配置保存失败: ' + err.message, true);
      if (statusText) {
        statusText.innerText = '✗ 保存失败: ' + err.message;
        statusText.style.color = 'var(--bx-accent-red)';
      }
    }
  }

  // =========================================================================
  // REQ-04: Open Project or File in Local IDE / AI Tool
  // =========================================================================
  async function openInSelectedIDE(relFile = '', line = 1) {
    const toolId = document.getElementById('bx-ide-selector')?.value || 'vscode';
    try {
      const res = await fetch('/api/ide/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tool_id: toolId,
          project_hash: currentHash,
          file: relFile,
          line: line,
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || ('HTTP ' + res.status));
      }
      if (data.mode === 'uri_scheme' && data.uri) {
        window.location.href = data.uri;
        showToast(`已通过 URI 协议唤起 ${toolId}`);
      } else {
        showToast(`已在本地 ${toolId} 中打开 ${relFile || '当前项目'}`);
      }
    } catch (err) {
      showToast('打开 IDE 失败: ' + err.message, true);
    }
  }
  window.openFileInIDE = openInSelectedIDE;

  // =========================================================================
  // REQ-06: Project Archive & Two-Mode Graded Deletion
  // =========================================================================
  async function toggleArchiveCurrentProject() {
    if (!currentHash) return;
    const curProj = projectsList.find(p => p.hash === currentHash);
    const curArchived = Boolean((currentSettings && currentSettings.archived !== undefined) ? currentSettings.archived : (curProj && curProj.archived));
    const nextArchived = !curArchived;
    try {
      const res = await fetch(`/projects/${currentHash}/archive`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: nextArchived }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '操作失败');
      if (currentSettings) currentSettings.archived = Boolean(data.archived);
      const archBtnTxt = document.getElementById('bx-archive-btn-text');
      if (archBtnTxt) archBtnTxt.innerText = data.archived ? '取消归档' : '归档';
      showToast(nextArchived ? '项目已归档折叠' : '项目已恢复至活跃列表');
      await fetchProjectsList();
      await loadProjectSettings(currentHash);
    } catch (err) {
      showToast('归档操作失败: ' + err.message, true);
    }
  }

  async function promptDeleteCurrentProject() {
    if (!currentHash) return;
    const choice = window.prompt(
      '请选择项目删除级别 (输入 remove 或 purge)：\n' +
      '1. remove — 仅从列表移除并加入防复活黑名单 (保留磁盘 .bugx 与源码)\n' +
      '2. purge  — 彻底清理项目下 .bugx 审计与会话目录 (100% 保留用户业务源码)',
      'remove'
    );
    if (!choice) return;
    const mode = choice.trim().toLowerCase() === 'purge' ? 'purge' : 'remove';
    try {
      const res = await fetch(`/projects/${currentHash}?mode=${mode}`, { method: 'DELETE' });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '删除失败');
      showToast(mode === 'purge' ? '已彻底清理 .bugx 数据并从列表移除' : '已从列表移除 (不会自动复活)');
      await fetchProjectsList();
      const firstHash = (projectsList[0] && projectsList[0].hash) || '';
      if (firstHash) {
        await loadProjectSettings(firstHash);
      } else {
        closeSettingsModal();
      }
    } catch (err) {
      showToast('删除项目失败: ' + err.message, true);
    }
  }

  // =========================================================================
  // REQ-08: Manual Task Cancellation Preserving Strategy Group Breakpoint
  // =========================================================================
  async function cancelActiveProjectTask() {
    try {
      const activeRes = await fetch('/chat/active');
      const activeList = activeRes.ok ? await activeRes.json() : [];
      const projSessions = activeList.filter(a => !currentHash || a.project_hash === currentHash);
      const targetSid = (projSessions[0] && projSessions[0].session_id) || 'session-default-01';
      const res = await fetch('/live/stop', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: targetSid, project_hash: currentHash }),
      });
      const data = await res.json();
      if (res.ok && data.accepted) {
        showToast('⏹️ 已终止运行进程组并释放租约，已完成策略组进度与漏洞已安全保留！');
        await refreshMiningDataOnly(true);
      } else {
        showToast('当前无运行中进程或已停止');
      }
    } catch (err) {
      showToast('取消任务失败: ' + err.message, true);
    }
  }

  // =========================================================================
  // REQ-09: MCP & Skill Marketplace, Toggle, Sandbox Credentials & Auto-Match
  // =========================================================================
  async function loadMcpMarketplace() {
    const grid = document.getElementById('bx-mcp-marketplace-grid');
    if (!grid) return;
    try {
      const res = await fetch(`/api/mcp/marketplace?project_hash=${encodeURIComponent(currentHash || '')}`);
      if (!res.ok) return;
      const data = await res.json();
      const items = data.items || [];
      grid.innerHTML = items.map(it => `
        <div class="bx-mkt-card ${it.mounted ? 'recommended' : ''}">
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <strong style="font-size: 0.86rem; color: var(--bx-text-main);">${it.title || it.id}</strong>
              <span class="bx-badge ${it.mounted ? 'bx-badge-green' : 'bx-badge-gray'}">${it.mounted ? '● 已挂载' : '未挂载'}</span>
            </div>
            <div style="font-size: 0.76rem; color: var(--bx-text-muted); margin-top: 4px;">${it.description || ''}</div>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
            <span style="font-size: 0.72rem; color: var(--bx-text-muted); font-family: monospace;">${(it.required_env && it.required_env.length) ? ('需凭据: ' + it.required_env.join(', ')) : '免凭据开箱即用'}</span>
            <div style="display: flex; gap: 6px;">
              ${(it.required_env && it.required_env.length) ? `<button type="button" class="bx-action-btn" onclick="window.promptConfigureMcp('${it.id}', '${it.required_env.join(',')}')"><span>${BxIcons.key(12)}</span> <span>凭据</span></button>` : ''}
              <button type="button" class="bx-action-btn ${it.mounted ? 'danger' : 'primary'}" onclick="window.toggleMcpServer('${it.id}', ${!it.mounted})">${it.mounted ? '卸载' : '一键挂载'}</button>
            </div>
          </div>
        </div>
      `).join('');
    } catch (e) {
      console.warn('[MCP Marketplace]', e);
    }
  }

  window.toggleMcpServer = async function (mcpId, enabled) {
    try {
      const res = await fetch('/api/mcp/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_hash: currentHash, mcp_id: mcpId, enabled: Boolean(enabled) }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '操作失败');
      showToast(enabled ? `已挂载 MCP: ${mcpId}` : `已卸载 MCP: ${mcpId}`);
      await loadProjectSettings(currentHash);
      await loadMcpMarketplace();
    } catch (err) {
      showToast('MCP 切换失败: ' + err.message, true);
    }
  };

  window.promptConfigureMcp = async function (mcpId, envKeysCsv = '') {
    const keyName = envKeysCsv ? envKeysCsv.split(',')[0] : window.prompt(`请输入 ${mcpId} 的环境变量名 (如 GITHUB_PERSONAL_ACCESS_TOKEN):`, 'API_KEY');
    if (!keyName) return;
    const secretVal = window.prompt(`请输入 ${mcpId} 的 ${keyName} 密钥值 (将加密隔离存储在 ~/.bugx/sandboxes/，绝不明文写入项目 .mcp.json):`, '');
    if (secretVal === null) return;
    try {
      const res = await fetch('/api/mcp/configure', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_hash: currentHash,
          mcp_id: mcpId,
          env: { [keyName.trim()]: secretVal },
        }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '配置失败');
      showToast(`凭据已安全隔离写入沙箱 (${data.sandbox_env_path || '0600'})`);
      await loadProjectSettings(currentHash);
      await loadMcpMarketplace();
    } catch (err) {
      showToast('凭据配置失败: ' + err.message, true);
    }
  };

  async function loadSkillMarketplace() {
    const grid = document.getElementById('bx-skill-marketplace-grid');
    if (!grid) return;
    try {
      const res = await fetch(`/api/skills/marketplace?project_hash=${encodeURIComponent(currentHash || '')}`);
      if (!res.ok) return;
      const data = await res.json();
      const items = (data.items || []).slice(0, 24);
      grid.innerHTML = items.map(it => `
        <div class="bx-mkt-card ${it.mounted ? 'recommended' : ''}">
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <strong style="font-size: 0.85rem; color: var(--bx-text-main);">${it.command || ('/' + it.name)}</strong>
              <span class="bx-badge ${it.enabled ? 'bx-badge-green' : 'bx-badge-gray'}">${it.enabled ? '● 已启用' : '未启用'}</span>
            </div>
            <div style="font-size: 0.75rem; color: var(--bx-text-muted); margin-top: 4px;">${it.description || ''}</div>
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
            <span class="bx-tag">${it.scope || 'global'}</span>
            <div style="display: flex; gap: 6px;">
              <button type="button" class="bx-action-btn" onclick="window.previewSkillMd('${it.name}')"><span>${BxIcons.file(12)}</span> <span>SKILL.md</span></button>
              <button type="button" class="bx-action-btn ${it.enabled ? 'danger' : 'primary'}" onclick="window.toggleSkillItem('${it.name}', ${!it.enabled})">${it.enabled ? '停用' : '挂载启用'}</button>
            </div>
          </div>
        </div>
      `).join('');
    } catch (e) {
      console.warn('[Skill Marketplace]', e);
    }
  }

  window.toggleSkillItem = async function (skillName, enabled) {
    try {
      const res = await fetch('/api/skills/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_hash: currentHash, skill_name: skillName, enabled: Boolean(enabled) }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '操作失败');
      showToast(enabled ? `已启用 Skill: ${skillName}` : `已停用 Skill: ${skillName}`);
      await loadProjectSettings(currentHash);
      await loadSkillMarketplace();
    } catch (err) {
      showToast('Skill 切换失败: ' + err.message, true);
    }
  };

  window.previewSkillMd = async function (skillName) {
    const card = document.getElementById('bx-skill-preview-card');
    const title = document.getElementById('bx-skill-preview-title');
    const box = document.getElementById('bx-skill-preview-content');
    const editBtn = document.getElementById('bx-skill-edit-ide-btn');
    if (!card || !box) return;
    try {
      const res = await fetch(`/api/skills/content?project_hash=${encodeURIComponent(currentHash || '')}&name=${encodeURIComponent(skillName)}`);
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '未找到 SKILL.md');
      card.style.display = 'block';
      const fpath = data.file_path || data.path || '';
      if (title) title.innerText = `${skillName} — SKILL.md (${fpath})`;
      box.textContent = data.content || '';
      if (editBtn) {
        editBtn.onclick = () => openInSelectedIDE(fpath, 1);
      }
    } catch (err) {
      showToast('读取 SKILL.md 失败: ' + err.message, true);
    }
  };

  async function runCapabilityAutoMatch(applyNow = true) {
    try {
      const res = await fetch('/api/capabilities/auto-match', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_hash: currentHash, apply: Boolean(applyNow) }),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || '匹配失败');
      const recMcps = (data.recommended_mcps || []).map(m => `${m.id} (${m.reason})`).join('；');
      const recSkills = (data.recommended_skills || []).map(s => `/${s.name} (${s.reason})`).join('；');
      const banner = document.getElementById('bx-automatch-banner');
      if (banner) {
        banner.style.display = 'block';
        banner.innerHTML = `
          <div><strong>智能匹配结果 (已自动挂载无凭据项: ${(data.applied_mcps || []).concat(data.applied_skills || []).join(', ') || '全部就绪'})</strong></div>
          <div style="margin-top:4px;"><strong>推荐 MCP:</strong> ${recMcps || '无'}</div>
          <div style="margin-top:2px;"><strong>推荐 Skill:</strong> ${recSkills || '无'}</div>
        `;
      }
      showToast('已完成项目画像智能匹配并挂载推荐的 MCP 与 Skill！');
      await loadProjectSettings(currentHash);
      await loadMcpMarketplace();
      await loadSkillMarketplace();
    } catch (err) {
      showToast('智能匹配失败: ' + err.message, true);
    }
  }

  // =========================================================================
  // REQ-05: Read-Only Project Document Center & FTS Search
  // =========================================================================
  async function loadProjectDocsTree() {
    const listEl = document.getElementById('bx-docs-list');
    const countEl = document.getElementById('bx-doc-count');
    if (!listEl) return;
    try {
      const res = await fetch(`/api/docs/tree?project_hash=${encodeURIComponent(currentHash || '')}`);
      if (!res.ok) return;
      const data = await res.json();
      const files = data.files || [];
      if (countEl) countEl.innerText = files.length;
      if (files.length === 0) {
        listEl.innerHTML = `<div style="color: var(--bx-text-muted); font-size: 0.78rem; padding: 8px;">暂无受管文档</div>`;
        return;
      }
      listEl.innerHTML = files.map(f => `
        <div class="bx-doc-item" data-doc-path="${f.path.replace(/"/g, '&quot;')}">
          <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; display: inline-flex; align-items: center; gap: 5px;" title="${f.path}"><span>${BxIcons.file(13)}</span> <span>${f.path}</span></span>
          <span class="bx-tag" style="font-size: 0.65rem;">${f.category}</span>
          <button type="button" class="bx-doc-del-btn" title="删除该受管文档" data-del-path="${f.path.replace(/"/g, '&quot;')}">${BxIcons.trash(12)}</button>
        </div>
      `).join('');
      listEl.querySelectorAll('.bx-doc-item').forEach(item => {
        item.onclick = (e) => {
          if (e.target.closest('.bx-doc-del-btn')) return;
          listEl.querySelectorAll('.bx-doc-item').forEach(i => i.classList.remove('active'));
          item.classList.add('active');
          openProjectDocContent(item.getAttribute('data-doc-path'));
        };
      });
      listEl.querySelectorAll('.bx-doc-del-btn').forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const docPath = btn.getAttribute('data-del-path');
          if (!confirm(`确认删除受管文档 "${docPath}" 吗？此操作无法撤销。`)) return;
          try {
            const res = await fetch('/api/docs/delete', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ path: docPath })
            });
            const data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
            showToast(`已删除文档: ${docPath}`);
            loadProjectDocsTree();
          } catch (err) {
            alert(`删除失败: ${err.message}`);
          }
        };
      });
      // Auto-select README.md or first doc
      const firstPath = files[0].path;
      openProjectDocContent(firstPath);
    } catch (e) {
      console.warn('[DocsTree]', e);
    }
  }

  async function openProjectDocContent(relPath) {
    if (!relPath) return;
    window._bxCurrentDocPath = relPath;
    const pathEl = document.getElementById('bx-doc-current-path');
    const boxEl = document.getElementById('bx-doc-content-box');
    if (pathEl) pathEl.innerText = relPath;
    if (boxEl) boxEl.textContent = '正在加载文档内容...';
    try {
      const res = await fetch(`/api/docs/content?project_hash=${encodeURIComponent(currentHash || '')}&path=${encodeURIComponent(relPath)}`);
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
      if (boxEl) boxEl.textContent = data.content || '';
    } catch (err) {
      if (boxEl) boxEl.textContent = '读取失败: ' + err.message;
    }
  }

  async function searchProjectDocs(query) {
    const q = (query || '').trim();
    if (!q) {
      loadProjectDocsTree();
      return;
    }
    const listEl = document.getElementById('bx-docs-list');
    try {
      const res = await fetch(`/api/docs/search?project_hash=${encodeURIComponent(currentHash || '')}&q=${encodeURIComponent(q)}`);
      if (!res.ok) return;
      const data = await res.json();
      const results = data.results || [];
      if (!listEl) return;
      if (results.length === 0) {
        listEl.innerHTML = `<div style="color: var(--bx-text-muted); font-size: 0.78rem; padding: 8px;">未找到匹配 "${q}" 的文档</div>`;
        return;
      }
      listEl.innerHTML = results.map(r => `
        <div class="bx-doc-item" data-doc-path="${r.path.replace(/"/g, '&quot;')}">
          <div>
            <div style="font-weight: 600; display: inline-flex; align-items: center; gap: 5px;"><span>${BxIcons.file(13)}</span> <span>${r.path}</span> <span style="font-size:0.75rem; color:var(--bx-text-muted);">(L${r.line})</span></div>
            <div style="font-size: 0.7rem; color: var(--bx-text-muted);">${r.snippet || ''}</div>
          </div>
        </div>
      `).join('');
      listEl.querySelectorAll('.bx-doc-item').forEach(item => {
        item.onclick = () => {
          openProjectDocContent(item.getAttribute('data-doc-path'));
        };
      });
    } catch (e) {
      console.warn('[DocsSearch]', e);
    }
  }

  // =========================================================================
  // REQ-10: Workspace File Management & Safe Deletion
  // =========================================================================
  let currentWsFilesPath = '';
  let cachedWsFiles = [];

  function formatFileSize(bytes) {
    if (bytes === 0 || bytes === null || bytes === undefined) return '-';
    if (typeof bytes !== 'number') return '-';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
  }

  function getFileIconAndType(name, isDir) {
    if (isDir) return { icon: BxIcons.folder(15), type: '文件夹' };
    const ext = name.split('.').pop().toLowerCase();
    const map = {
      py: { icon: BxIcons.fileCode(15), type: 'Python 源文件' },
      js: { icon: BxIcons.fileCode(15), type: 'JavaScript' },
      ts: { icon: BxIcons.fileCode(15), type: 'TypeScript' },
      jsx: { icon: BxIcons.fileCode(15), type: 'React JSX' },
      tsx: { icon: BxIcons.fileCode(15), type: 'React TSX' },
      json: { icon: BxIcons.settings(15), type: 'JSON 配置' },
      md: { icon: BxIcons.book(15), type: 'Markdown 文档' },
      html: { icon: BxIcons.fileCode(15), type: 'HTML 页面' },
      css: { icon: BxIcons.fileCode(15), type: 'CSS 样式' },
      txt: { icon: BxIcons.file(15), type: '文本文件' },
      png: { icon: BxIcons.file(15), type: 'PNG 图像' },
      jpg: { icon: BxIcons.file(15), type: 'JPG 图像' },
      jpeg: { icon: BxIcons.file(15), type: 'JPG 图像' },
      svg: { icon: BxIcons.fileCode(15), type: 'SVG 矢量图' },
      sh: { icon: BxIcons.fileCode(15), type: 'Shell 脚本' },
      yml: { icon: BxIcons.settings(15), type: 'YAML 配置' },
      yaml: { icon: BxIcons.settings(15), type: 'YAML 配置' },
      toml: { icon: BxIcons.settings(15), type: 'TOML 配置' },
      lock: { icon: BxIcons.shield(15), type: 'Lockfile' },
      log: { icon: BxIcons.file(15), type: '日志文件' },
    };
    return map[ext] || { icon: BxIcons.file(15), type: ext ? (ext.toUpperCase() + ' 文件') : '文件' };
  }

  async function loadWorkspaceFiles(subPath = '') {
    currentWsFilesPath = subPath || '';
    const tbody = document.getElementById('bx-ws-files-tbody');
    const emptyEl = document.getElementById('bx-ws-files-empty');
    if (!tbody) return;

    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; padding: 24px; color: var(--bx-text-muted);">正在加载工作区文件...</td></tr>`;
    if (emptyEl) emptyEl.style.display = 'none';

    renderWorkspaceBreadcrumbs(currentWsFilesPath);

    try {
      const res = await fetch(`/fs/list?path=${encodeURIComponent(currentWsFilesPath)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const entries = data.entries || [];
      cachedWsFiles = entries;
      renderWorkspaceFilesTable(entries);
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; padding: 24px; color: var(--bx-danger);">加载文件失败: ${escapeHtml(err.message)}</td></tr>`;
    }
  }

  function renderWorkspaceBreadcrumbs(currentPath) {
    const el = document.getElementById('bx-ws-files-breadcrumb');
    if (!el) return;

    const parts = (currentPath || '').split('/').filter(Boolean);
    let html = `<button type="button" class="bx-crumb-btn" data-subpath="">${BxIcons.home(13)} 工作区根</button>`;
    let accumulated = '';
    for (let i = 0; i < parts.length; i++) {
      accumulated += (accumulated ? '/' : '') + parts[i];
      const isLast = (i === parts.length - 1);
      html += `<span class="bx-crumb-sep">/</span>`;
      if (isLast) {
        html += `<span class="bx-crumb-current">${escapeHtml(parts[i])}</span>`;
      } else {
        html += `<button type="button" class="bx-crumb-btn" data-subpath="${escapeHtml(accumulated)}">${escapeHtml(parts[i])}</button>`;
      }
    }
    el.innerHTML = html;
    el.querySelectorAll('.bx-crumb-btn').forEach(btn => {
      btn.onclick = () => {
        loadWorkspaceFiles(btn.getAttribute('data-subpath'));
      };
    });
  }

  function renderWorkspaceFilesTable(entries) {
    const tbody = document.getElementById('bx-ws-files-tbody');
    const emptyEl = document.getElementById('bx-ws-files-empty');
    if (!tbody) return;

    if (!entries || entries.length === 0) {
      tbody.innerHTML = '';
      if (emptyEl) emptyEl.style.display = 'block';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';

    // Sort: directories first, then alphabetical
    const sorted = [...entries].sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });

    let html = '';
    // If not root, add a ".." parent row
    if (currentWsFilesPath) {
      const parentParts = currentWsFilesPath.split('/').filter(Boolean);
      parentParts.pop();
      const parentSubPath = parentParts.join('/');
      html += `
        <tr class="bx-file-row bx-file-parent-row" data-subpath="${escapeHtml(parentSubPath)}">
          <td colspan="4" style="cursor: pointer; padding: 8px 12px; font-weight: 600; color: var(--bx-accent);">
            ${BxIcons.levelUp(14)} <strong>.. (返回上级目录)</strong>
          </td>
        </tr>
      `;
    }

    sorted.forEach(item => {
      const { icon, type } = getFileIconAndType(item.name, item.is_dir);
      const targetSubPath = currentWsFilesPath ? `${currentWsFilesPath}/${item.name}` : item.name;
      const fullPath = item.full || targetSubPath;
      const sizeStr = item.is_dir ? '-' : formatFileSize(item.size);

      html += `
        <tr class="bx-file-row" data-name="${escapeHtml(item.name)}" data-is-dir="${item.is_dir ? '1' : '0'}" data-full="${escapeHtml(fullPath)}" data-subpath="${escapeHtml(targetSubPath)}">
          <td class="bx-file-name-cell">
            <span class="bx-file-icon">${icon}</span>
            <span class="bx-file-title ${item.is_dir ? 'bx-dir-link' : ''}" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
          </td>
          <td class="bx-file-type-cell">${type}</td>
          <td class="bx-file-size-cell">${sizeStr}</td>
          <td class="bx-file-ops-cell">
            <div class="bx-file-ops">
              ${!item.is_dir ? `<button type="button" class="bx-file-action-btn bx-file-open-btn" title="在 IDE 中打开此文件">${BxIcons.ide(12)} 打开</button>` : ''}
              <button type="button" class="bx-file-action-btn danger bx-file-del-btn" title="删除该${item.is_dir ? '文件夹及全部内容' : '文件'}">${BxIcons.trash(12)} 删除</button>
            </div>
          </td>
        </tr>
      `;
    });

    tbody.innerHTML = html;

    // Bind row clicks
    tbody.querySelectorAll('.bx-file-parent-row').forEach(row => {
      row.onclick = () => {
        loadWorkspaceFiles(row.getAttribute('data-subpath'));
      };
    });

    tbody.querySelectorAll('.bx-file-row:not(.bx-file-parent-row)').forEach(row => {
      const isDir = row.getAttribute('data-is-dir') === '1';
      const subpath = row.getAttribute('data-subpath');
      const full = row.getAttribute('data-full');
      const name = row.getAttribute('data-name');

      if (isDir) {
        const link = row.querySelector('.bx-dir-link');
        if (link) {
          link.onclick = (e) => {
            e.stopPropagation();
            loadWorkspaceFiles(subpath);
          };
        }
        row.ondblclick = () => {
          loadWorkspaceFiles(subpath);
        };
      }

      const openBtn = row.querySelector('.bx-file-open-btn');
      if (openBtn) {
        openBtn.onclick = (e) => {
          e.stopPropagation();
          openInSelectedIDE(subpath);
        };
      }

      const delBtn = row.querySelector('.bx-file-del-btn');
      if (delBtn) {
        delBtn.onclick = (e) => {
          e.stopPropagation();
          promptDeleteWorkspaceFile(full || subpath, isDir, name);
        };
      }
    });
  }

  function filterWorkspaceFiles(query) {
    const q = (query || '').toLowerCase().trim();
    if (!q) {
      renderWorkspaceFilesTable(cachedWsFiles);
      return;
    }
    const filtered = cachedWsFiles.filter(item => item.name.toLowerCase().includes(q));
    renderWorkspaceFilesTable(filtered);
  }

  async function promptCreateWorkspaceFolder(parentSubPath) {
    const folderName = window.prompt('请输入要在当前目录下新建的文件夹名称:');
    if (!folderName || !folderName.trim()) return;
    const name = folderName.trim();
    if (name.includes('/') || name.includes('\\')) {
      alert('文件夹名称不能包含斜杠路径分隔符');
      return;
    }
    const targetPath = parentSubPath ? `${parentSubPath}/${name}` : name;
    try {
      const res = await fetch('/fs/mkdir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: targetPath })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      showToast(`已创建文件夹: ${name}`);
      loadWorkspaceFiles(parentSubPath);
    } catch (err) {
      alert(`创建文件夹失败: ${err.message}`);
    }
  }

  async function promptDeleteWorkspaceFile(targetPath, isDir, name) {
    const targetType = isDir ? '文件夹及其内部所有内容' : '文件';
    const confirmed = window.confirm(`安全确认：\n\n您确定要彻底删除该${targetType}吗？\n\n目标: "${name}"\n路径: ${targetPath}\n\n此操作不可撤销，请谨慎操作！`);
    if (!confirmed) return;

    try {
      const res = await fetch('/fs/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: targetPath })
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
      showToast(`已成功删除: ${name}`);
      loadWorkspaceFiles(currentWsFilesPath);
    } catch (err) {
      alert(`删除失败: ${err.message}`);
    }
  }

  function openSettingsModal(projHash = '', initialTab = '') {
    createModalDOM();
    document.body.classList.add('bx-modal-open');
    const overlay = document.getElementById('bx-settings-overlay');
    if (overlay) {
      overlay.style.display = 'flex';
      overlay.classList.add('active');
    }
    if (initialTab) {
      const targetBtn = document.querySelector(`.bx-tab-btn[data-tab="${initialTab}"]`);
      if (targetBtn) targetBtn.click();
    }
    loadProjectSettings(projHash);
    fetchProjectsList().then(() => {
      const sel = document.getElementById('bx-proj-selector');
      if (sel && currentHash) sel.value = currentHash;
    });
  }

  function closeSettingsModal() {
    stopLiveMonitoring();
    document.body.classList.remove('bx-modal-open');
    const overlay = document.getElementById('bx-settings-overlay');
    if (overlay) {
      overlay.classList.remove('active');
      overlay.style.display = 'none';
    }
  }

  window.openProjectSettings = openSettingsModal;
  window.openSettingsModal = openSettingsModal;
  window.closeProjectSettings = closeSettingsModal;
  window.insertSkillToChat = function (cmd) {
    closeSettingsModal();
    const textarea = document.querySelector('textarea, input[type="text"].chat-input');
    if (textarea) {
      textarea.value = cmd + ' ';
      textarea.focus();
      showToast(`已填入技能指令: ${cmd}`);
    } else {
      showToast(`已选择技能: ${cmd}`);
    }
  };

  // =========================================================================
  // Composer Host Brand Icons & Independent Model Picker Pill (REQ-01 Polish)
  // =========================================================================
  const HOST_BRAND_SVGS = {
    // 1. Cline 官方机器人头盔与大眼 (Official Robot Avatar)
    cline: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="24" height="24" rx="5.5" fill="#5B50E6"/><g transform="translate(2.5, 2.5) scale(0.79)"><path d="M17.035 3.991c2.75 0 4.98 2.24 4.98 5.003v1.667l1.45 2.896a1.01 1.01 0 01-.002.909l-1.448 2.864v1.668c0 2.762-2.23 5.002-4.98 5.002H7.074c-2.751 0-4.98-2.24-4.98-5.002V17.33l-1.48-2.855a1.01 1.01 0 01-.003-.927l1.482-2.887V8.994c0-2.763 2.23-5.003 4.98-5.003h9.962zM8.265 9.6a2.274 2.274 0 00-2.274 2.274v4.042a2.274 2.274 0 004.547 0v-4.042A2.274 2.274 0 008.265 9.6zm7.326 0a2.274 2.274 0 00-2.274 2.274v4.042a2.274 2.274 0 104.548 0v-4.042A2.274 2.274 0 0015.59 9.6z" fill="#FFFFFF"/><path d="M12.054 5.558a2.779 2.779 0 100-5.558 2.779 2.779 0 000 5.558z" fill="#A5B4FC"/></g></svg>`,

    // 2. OpenAI / Codex 官方螺旋花盘 (Official OpenAI Rosette)
    codex: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="24" height="24" rx="5.5" fill="#10A37F"/><path d="M9.205 8.658v-2.31c0-.19.095-.38.286-.476l4.662-2.688c.761-.428 1.665-.523 2.474-.238.808.286 1.474.857 1.855 1.618.761 1.475.476 3.283-.713 4.425v.714l2.141 1.237c.761.428 1.332 1.142 1.617 1.951.286.81.238 1.713-.19 2.474-.714 1.333-2.141 2.141-3.664 2.141h-.761v2.332c0 .19-.095.38-.286.476l-4.662 2.688c-.761.428-1.665.523-2.474.238-.808-.285-1.475-.856-1.855-1.618-.762-1.475-.476-3.283.713-4.425v-.714l-2.14-1.237c-.762-.428-1.333-1.142-1.618-1.951-.285-.81-.238-1.713.19-2.474.714-1.333 2.141-2.141 3.664-2.141h.761zm4.853 10.514v-4.52l-3.9-2.26-1.95 1.118v4.544l5.85 1.118zm2.664-6.423l-3.9-2.26v-4.52l-1.95 1.118-1.95 1.119v4.52l3.9 2.26 3.9-2.237zm-7.8-2.26l3.9 2.26 3.9-2.26-1.95-1.118-3.9-2.26-1.95 1.118zm1.95-3.38l-3.9 2.26v4.519l1.95-1.118 3.9-2.26v-4.52l-1.95 1.119zm4.519 1.119l1.95 1.118v4.52l-3.9 2.26-1.95-1.119v-4.52l3.9-2.259z" fill="#FFFFFF"/></svg>`,

    // 3. Anthropic Claude 官方 14 瓣星芒火花 (Official Claude Spark)
    claude: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z" fill="#D97757"/></svg>`,

    // 4. OpenCode 官方双层代码视窗 (Official OpenCode Window)
    opencode: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="24" height="24" rx="5.5" fill="#0284C7"/><path fill-rule="evenodd" clip-rule="evenodd" d="M15 7H9v10h6V7zm3 13H6V4h12v16z" fill="#FFFFFF"/></svg>`,

    // 5. Inflection Pi 官方优雅流线 π 符号 (Official Pi Brand)
    pi: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><rect width="24" height="24" rx="5.5" fill="#E11D48"/><path d="M6.5 7.8h11M9.2 7.8v8.4M14.8 7.8v6.8c0 1.1.7 1.6 1.6 1.6" stroke="#FFFFFF" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,

    // 6. 智谱 BigModel 官方立体棱镜水晶宝石 (Official BigModel Crystal Gem)
    zcode: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="bx-bm-top" x1="4" y1="2" x2="20" y2="10" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#7C9CFF"/><stop offset="100%" stop-color="#4F75FF"/></linearGradient><linearGradient id="bx-bm-left" x1="2" y1="7" x2="12" y2="22" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#00A3FF"/><stop offset="100%" stop-color="#0066FF"/></linearGradient><linearGradient id="bx-bm-right" x1="12" y1="7" x2="22" y2="22" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#5B6BFF"/><stop offset="100%" stop-color="#2B36D9"/></linearGradient></defs><path d="M12 2.2L19.8 6.7L12 11.2L4.2 6.7L12 2.2Z" fill="url(#bx-bm-top)"/><path d="M4.2 6.7L12 11.2V21.8L4.2 16.3V6.7Z" fill="url(#bx-bm-left)"/><path d="M19.8 6.7L12 11.2V21.8L19.8 16.3V6.7Z" fill="url(#bx-bm-right)"/><path d="M12 7.2L15.6 9.3L12 13.5L8.4 9.3L12 7.2Z" fill="#FFFFFF" fill-opacity="0.36"/></svg>`,

    // 7. DeepSeek 官方跃水深蓝鲸鱼 (Official DeepSeek Whale)
    dsh: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M23.748 4.482c-.254-.124-.364.113-.512.234-.051.039-.094.09-.137.136-.372.397-.806.657-1.373.626-.829-.046-1.537.214-2.163.848-.133-.782-.575-1.248-1.247-1.548-.352-.156-.708-.311-.955-.65-.172-.241-.219-.51-.305-.774-.055-.16-.11-.323-.293-.35-.2-.031-.278.136-.356.276-.313.572-.434 1.202-.422 1.84.027 1.436.633 2.58 1.838 3.393.137.093.172.187.129.323-.082.28-.18.552-.266.833-.055.179-.137.217-.329.14a5.526 5.526 0 01-1.736-1.18c-.857-.828-1.631-1.742-2.597-2.458a11.365 11.365 0 00-.689-.471c-.985-.957.13-1.743.388-1.836.27-.098.093-.432-.779-.428-.872.004-1.67.295-2.687.684a3.055 3.055 0 01-.465.137 9.597 9.597 0 00-2.883-.102c-1.885.21-3.39 1.102-4.497 2.623C.082 8.606-.231 10.684.152 12.85c.403 2.284 1.569 4.175 3.36 5.653 1.858 1.533 3.997 2.284 6.438 2.14 1.482-.085 3.133-.284 4.994-1.86.47.234.962.327 1.78.397.63.059 1.236-.03 1.705-.128.735-.156.684-.837.419-.961-2.155-1.004-1.682-.595-2.113-.926 1.096-1.296 2.746-2.642 3.392-7.003.05-.347.007-.565 0-.845-.004-.17.035-.237.23-.256a4.173 4.173 0 001.545-.475c1.396-.763 1.96-2.015 2.093-3.517.02-.23-.004-.467-.247-.588zM11.581 18c-2.089-1.642-3.102-2.183-3.52-2.16-.392.024-.321.471-.235.763.09.288.207.486.371.739.114.167.192.416-.113.603-.673.416-1.842-.14-1.897-.167-1.361-.802-2.5-1.86-3.301-3.307-.774-1.393-1.224-2.887-1.298-4.482-.02-.386.093-.522.477-.592a4.696 4.696 0 011.529-.039c2.132.312 3.946 1.265 5.468 2.774.868.86 1.525 1.887 2.202 2.891.72 1.066 1.494 2.082 2.48 2.914.348.292.625.514.891.677-.802.09-2.14.11-3.054-.614zm1-6.44a.306.306 0 01.415-.287.302.302 0 01.2.288.306.306 0 01-.31.307.303.303 0 01-.304-.308zm3.11 1.596c-.2.081-.399.151-.59.16a1.245 1.245 0 01-.798-.254c-.274-.23-.47-.358-.552-.758a1.73 1.73 0 01.016-.588c.07-.327-.008-.537-.239-.727-.187-.156-.426-.199-.688-.199a.559.559 0 01-.254-.078c-.11-.054-.2-.19-.114-.358.028-.054.16-.186.192-.21.356-.202.767-.136 1.146.016.352.144.618.408 1.001.782.391.451.462.576.685.914.176.265.336.537.445.848.067.195-.019.354-.25.452z" fill="#4D6BFE"/></svg>`,

    // 8. Google Gemini 官方四角渐变流光星芒 (Official Gemini Spark)
    google: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="bx-gemini-grad" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#1BA1E3"/><stop offset="50%" stop-color="#5460E6"/><stop offset="100%" stop-color="#9C52E4"/></linearGradient></defs><path d="M20.616 10.835a14.147 14.147 0 01-4.45-3.376 13.785 13.785 0 01-3.37-4.456C12.57 2.477 12.336 2 12 2s-.57.477-.796 1.003a13.79 13.79 0 01-3.37 4.456 14.14 14.14 0 01-4.45 3.376C2.858 11.06 2 11.295 2 12c0 .705.858.94 1.384 1.165a14.143 14.143 0 014.45 3.376 13.785 13.785 0 013.37 4.456C11.43 21.523 11.664 22 12 22s.57-.477.796-1.003a13.78 13.78 0 013.37-4.456 14.143 14.143 0 014.45-3.376C21.142 12.94 22 12.705 22 12c0-.705-.858-.94-1.384-1.165z" fill="url(#bx-gemini-grad)"/></svg>`,

    // 9. BugX Engine 官方攻防引擎安全科技护盾 (Official BugX Shield)
    bugx: `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="bx-eng-grad" x1="2" y1="2" x2="22" y2="22" gradientUnits="userSpaceOnUse"><stop offset="0%" stop-color="#0284C7"/><stop offset="100%" stop-color="#10B981"/></linearGradient></defs><path d="M12 2L20.5 5.5V11.5C20.5 16.5 16.8 20.8 12 22C7.2 20.8 3.5 16.5 3.5 11.5V5.5L12 2Z" fill="url(#bx-eng-grad)"/><path d="M9.5 12L11.5 14L15.5 10" stroke="#FFFFFF" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  };

  const VENDOR_ICON_SVGS = {
    anthropic: HOST_BRAND_SVGS.claude,
    openai: HOST_BRAND_SVGS.codex,
    deepseek: HOST_BRAND_SVGS.dsh,
    google: HOST_BRAND_SVGS.google,
    gemini: HOST_BRAND_SVGS.google,
    zhipu: HOST_BRAND_SVGS.zcode,
    zcode: HOST_BRAND_SVGS.zcode,
    glm: HOST_BRAND_SVGS.zcode,
    bigmodel: HOST_BRAND_SVGS.zcode,
    bugx: HOST_BRAND_SVGS.bugx,
    auto: `<svg viewBox="0 0 20 20" fill="none"><rect x="1.5" y="1.5" width="17" height="17" rx="4.5" fill="#64748B"/><circle cx="10" cy="10" r="3.2" stroke="#fff" stroke-width="1.7"/></svg>`,
    '9router': HOST_BRAND_SVGS.pi,
    pi: HOST_BRAND_SVGS.pi,
    cline: HOST_BRAND_SVGS.cline,
    'cline-pass': HOST_BRAND_SVGS.cline,
    opencode: HOST_BRAND_SVGS.opencode,
    'opencode-go': HOST_BRAND_SVGS.opencode,
    kimi: `<svg viewBox="0 0 20 20" fill="none"><rect x="1.5" y="1.5" width="17" height="17" rx="4.5" fill="#000"/><path d="M6 6h2.5v8H6V6zm4 0h4v2.5h-4V6zm0 5.5h4V14h-4v-2.5z" fill="#fff"/></svg>`,
    grok: `<svg viewBox="0 0 20 20" fill="none"><rect x="1.5" y="1.5" width="17" height="17" rx="4.5" fill="#000"/><path d="M5.5 5.5l9 9m0-9l-9 9" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/></svg>`,
    sensenova: `<svg viewBox="0 0 20 20" fill="none"><rect x="1.5" y="1.5" width="17" height="17" rx="4.5" fill="#E60012"/><circle cx="10" cy="10" r="4.5" fill="#fff"/></svg>`,
    '商汤': `<svg viewBox="0 0 20 20" fill="none"><rect x="1.5" y="1.5" width="17" height="17" rx="4.5" fill="#E60012"/><circle cx="10" cy="10" r="4.5" fill="#fff"/></svg>`,
    dpf: HOST_BRAND_SVGS.dsh,
  };

  const IDE_BRAND_ICONS = {
    vscode: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><path d="M17.5 2.5L7.2 10.3 3 7.2l-2 1.3 3.6 3.5-3.6 3.5 2 1.3 4.2-3.1L17.5 21.5l4.5-2.2V4.7L17.5 2.5zm0 4.3v10.4l-6.8-5.2 6.8-5.2z" fill="#007ACC"/></svg>`,
    idea: `<svg viewBox="0 0 24 24" width="16" height="16"><rect width="24" height="24" rx="4" fill="#000"/><path d="M4 17.5h6v1.8H4z" fill="#fff"/><path d="M4 4.5h2.5v7H4z" fill="#FE2857"/><path d="M8.5 4.5h6v2.2h-6z" fill="#007ACC"/><path d="M11 6.7h3.5v4.8H11z" fill="#35D073"/></svg>`,
    cursor: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect width="24" height="24" rx="4" fill="#000"/><path d="M6 5.5l11 6.5-6.5 1.5-1.5 6.5L6 5.5z" fill="#fff"/></svg>`,
    trae: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect width="24" height="24" rx="4" fill="#18181B"/><circle cx="12" cy="12" r="5" stroke="#38BDF8" stroke-width="2"/><path d="M9 12h6" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>`,
    pycharm: `<svg viewBox="0 0 24 24" width="16" height="16"><rect width="24" height="24" rx="4" fill="#000"/><path d="M4 17.5h6v1.8H4z" fill="#fff"/><path d="M4 4.5h6v2.2H4zm0 2.2h2.5v5H4z" fill="#21D789"/><path d="M11 4.5h5.5l-3.2 7h-2.3z" fill="#FCFC00"/></svg>`,
    zed: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect width="24" height="24" rx="4" fill="#0A0A0A"/><path d="M7 7h10l-10 10h10" stroke="#F59E0B" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
    qoder: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect width="24" height="24" rx="4" fill="#2563EB"/><path d="M7 8h10M7 12h10M7 16h6" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>`,
    antigravity: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect width="24" height="24" rx="4" fill="#4338CA"/><circle cx="12" cy="12" r="6" stroke="#A5B4FC" stroke-width="2"/><path d="M12 6v12M6 12h12" stroke="#fff" stroke-width="1.8"/></svg>`,
    finder: `<svg viewBox="0 0 24 24" width="16" height="16" fill="none"><rect x="2" y="3" width="20" height="18" rx="4" fill="#007AFF"/><path d="M8 8v1M16 8v1M8 15c2 2 6 2 8 0" stroke="#fff" stroke-width="2" stroke-linecap="round"/><path d="M12 3v10" stroke="#fff" stroke-width="2"/></svg>`,
  };

  function getIdeBrandIcon(id) {
    return IDE_BRAND_ICONS[id] || BxIcons.ide(16);
  }

  let composerCatalog = [];
  let selectedHostProvider = 'codex';
  let selectedHostModels = {}; // provider -> model_id

  function detectProviderKeyFromText(txt) {
    const s = String(txt || '').toLowerCase();
    if (s.includes('cline')) return 'cline';
    if (s.includes('codex')) return 'codex';
    if (s.includes('claude')) return 'claude';
    if (s.includes('opencode')) return 'opencode';
    if (s.includes('pi')) return 'pi';
    if (s.includes('zcode')) return 'zcode';
    if (s.includes('dsh')) return 'dsh';
    if (s.includes('bugx')) return 'bugx';
    return '';
  }

  function decorateHostSelectorIcons() {
    const controls = document.querySelector('.model-controls');
    if (!controls) return;

    // 强力隐藏原生宿主与思考强度选择器，避免双图标与文字重叠
    controls.querySelectorAll('.effort-selector, .model-selector:not(.bx-unified-model-picker)').forEach(el => {
      el.style.setProperty('display', 'none', 'important');
    });

    const selectors = controls.querySelectorAll('.model-selector:not(.effort-selector):not(.bx-unified-model-picker)');
    const hostSelector = selectors[selectors.length - 1];
    if (hostSelector) {
      hostSelector.style.setProperty('display', 'none', 'important');
      const labelEl = hostSelector.querySelector('.model-selector-label');
      if (labelEl) {
        const detected = detectProviderKeyFromText(labelEl.textContent);
        if (detected && detected !== selectedHostProvider) {
          selectedHostProvider = detected;
          const active = getActiveModelOption(detected);
          selectedHostModels[detected] = active.id;
          renderComposerModelPill();
        }
      }
    }
  }

  function getActiveModelOption(providerKey) {
    const entry = composerCatalog.find(c => c.provider === providerKey) || composerCatalog[0];
    if (!entry) {
      return { id: '', label: '未配置', vendor: 'auto', options: [] };
    }
    const opts = Array.isArray(entry.model_options) && entry.model_options.length
      ? entry.model_options
      : (entry.model_id ? [{ id: entry.model_id, label: entry.model_id, vendor: 'auto' }] : []);
    if (!opts.length) {
      return { id: '', label: '未探查到模型', vendor: 'auto', options: [], hostEntry: entry };
    }
    const chosenId = selectedHostModels[providerKey] || entry.model_id || opts[0].id;
    let matched = opts.find(o => o.id === chosenId);
    if (!matched) {
      matched = opts.find(o => o.id === entry.model_id) || opts[0];
      if (matched) selectedHostModels[providerKey] = matched.id;
    }
    return { ...matched, options: opts, hostEntry: entry };
  }

  async function syncComposerModelPreference() {
    const active = getActiveModelOption(selectedHostProvider);
    try {
      const res = await fetch('/live/provider', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: selectedHostProvider,
          model: active.id,
        }),
      });
      if (res.ok) {
        const mRes = await fetch('/models');
        if (mRes.ok) {
          const list = await mRes.json();
          if (Array.isArray(list) && list.length) {
            composerCatalog = list;
          }
        }
      }
    } catch (e) {
      console.warn('[ComposerModelPicker] sync failed:', e);
    }
  }

  const HOST_DISPLAY_INFO = {
    zcode: { name: 'BigModel', badge: '个人', title: '智谱 BigModel' },
    codex: { name: 'Codex', badge: '官方', title: 'OpenAI Codex' },
    claude: { name: 'Claude Code', badge: '官方', title: 'Anthropic Claude' },
    cline: { name: 'Cline', badge: '扩展', title: 'Cline Extension' },
    opencode: { name: 'OpenCode', badge: '开源', title: 'OpenCode CLI' },
    pi: { name: 'Pi Agent', badge: '多路由', title: 'Pi Multi-Agent' },
    dsh: { name: 'DSH Agent', badge: '已配置', title: 'DeepSeek Hybrid' },
    bugx: { name: 'BugX Engine', badge: '原生核心', title: 'BugX 攻防引擎' },
  };

  const EFFORT_LEVELS = [
    { key: 'off', chinese: '默认', label: '默认', pos: '10px', percent: 0 },
    { key: 'low', chinese: '低', label: 'Low', pos: '25%', percent: 25 },
    { key: 'medium', chinese: '中', label: 'Medium', pos: '50%', percent: 50 },
    { key: 'high', chinese: '高', label: 'High', pos: '75%', percent: 75 },
    { key: 'max', chinese: '最高', label: 'Max', pos: 'calc(100% - 10px)', percent: 100 },
  ];

  const CHECKMARK_SVG = `<svg width="18" height="18" viewBox="0 0 20 20" fill="none"><path d="M4 10.5L8 14.5L16 6.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

  let currentEffortIndex = -1;

  function getCurrentEffortIndex() {
    if (currentEffortIndex >= 0) return currentEffortIndex;
    const label = document.querySelector('.effort-selector .model-selector-label')?.textContent?.trim()?.toLowerCase() || '';
    if (label.includes('max') || label.includes('xhigh') || label.includes('最高')) return 4;
    if (label.includes('high') || label.includes('高')) return 3;
    if (label.includes('medium') || label.includes('中')) return 2;
    if (label.includes('low') || label.includes('低')) return 1;
    return 0; // 默认
  }

  function applyEffortLevel(levelIdx) {
    currentEffortIndex = levelIdx;
    const lvl = EFFORT_LEVELS[levelIdx] || EFFORT_LEVELS[0];
    const knob = document.getElementById('bx-ump-slider-knob');
    const valEl = document.getElementById('bx-ump-effort-val');
    if (knob) knob.style.left = lvl.pos;
    if (valEl) valEl.textContent = lvl.chinese;

    // Trigger underlying .effort-selector
    const effortSel = document.querySelector('.effort-selector');
    if (!effortSel) return;

    const clickMatchingItem = (dd) => {
      const items = Array.from(dd.querySelectorAll('.model-item'));
      const target = lvl.label.toLowerCase();
      const matched = items.find(it => {
        const txt = it.textContent.trim().toLowerCase();
        if (target === 'max') return txt === 'max' || txt === 'xhigh' || txt.includes('max');
        if (target === 'high') return txt === 'high' && !txt.includes('xhigh');
        return txt === target || txt.includes(target);
      });
      if (matched) {
        matched.click();
      }
    };

    let dd = effortSel.querySelector('.model-dropdown');
    if (dd) {
      clickMatchingItem(dd);
    } else {
      const trigger = effortSel.querySelector('.model-selector-trigger');
      if (trigger) {
        trigger.click();
        setTimeout(() => {
          dd = effortSel.querySelector('.model-dropdown');
          if (dd) clickMatchingItem(dd);
        }, 60);
      }
    }
  }

  function positionPopover(popover, triggerBtn) {
    if (!popover || !triggerBtn) return;
    const btnRect = triggerBtn.getBoundingClientRect();
    const vh = window.innerHeight;

    // 默认行为：底栏工具永远向上展开 (bottom: calc(100% + 8px))，杜绝被屏幕底部遮挡
    // 只有在屏幕顶部空间极其狭小 (spaceAbove < 200) 且下方空间明显充裕 (spaceBelow >= 340) 时，才向下展开
    const spaceAbove = btnRect.top;
    const spaceBelow = vh - btnRect.bottom;
    const shouldGoDown = spaceAbove < 200 && spaceBelow >= 340;

    if (shouldGoDown) {
      popover.style.bottom = 'auto';
      popover.style.top = 'calc(100% + 8px)';
      const maxH = Math.max(200, Math.floor(spaceBelow - 20));
      popover.style.maxHeight = `${maxH}px`;
    } else {
      popover.style.top = 'auto';
      popover.style.bottom = 'calc(100% + 8px)';
      // 向上展开时约束最大高度，绝不冲出屏幕顶边，底部紧贴按钮，彻底根除底部遮挡
      const maxH = Math.max(200, Math.floor(spaceAbove - 20));
      popover.style.maxHeight = `${maxH}px`;
    }
    popover.style.overflowY = 'auto';
    popover.style.overscrollBehavior = 'contain';
  }

  function selectHostProvider(providerKey) {
    selectedHostProvider = providerKey;
    const hostSel = document.querySelector('.model-controls > .model-selector:not(.effort-selector):not(.bx-unified-model-picker)');
    if (hostSel) {
      const trigger = hostSel.querySelector('.model-selector-trigger');
      if (trigger) {
        trigger.click();
        setTimeout(() => {
          const dd = hostSel.querySelector('.model-dropdown');
          if (dd) {
            const items = Array.from(dd.querySelectorAll('.model-item'));
            const matched = items.find(it => {
              const p = (it.getAttribute('title') || detectProviderKeyFromText(it.textContent) || '').toLowerCase();
              return p === providerKey;
            });
            if (matched) matched.click();
          }
        }, 30);
      }
    }
    const active = getActiveModelOption(providerKey);
    selectedHostModels[providerKey] = active.id;
    const picker = document.getElementById('bx-composer-model-picker');
    if (picker) picker.removeAttribute('data-state-key');
    renderComposerModelPill();
    syncComposerModelPreference();
    const hInfo = HOST_DISPLAY_INFO[providerKey] || { name: providerKey };
    showToast(`切换宿主平台: ${hInfo.name}`);
  }

  if (!window._bxEffortShortcutBound) {
    window._bxEffortShortcutBound = true;
    window.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 't' || e.key === 'T')) {
        e.preventDefault();
        const curIdx = getCurrentEffortIndex();
        const nextIdx = (curIdx + 1) % EFFORT_LEVELS.length;
        applyEffortLevel(nextIdx);
        showToast(`思考强度: ${EFFORT_LEVELS[nextIdx].chinese} (${EFFORT_LEVELS[nextIdx].label})`);
      }
    });
  }

  function enhanceModeSelector() {
    const modeSel = document.querySelector('.input-footer-actions > .mode-selector');
    if (!modeSel) return;
    const trigger = modeSel.querySelector('.mode-selector-trigger');
    if (!trigger) return;

    if (!trigger.classList.contains('bx-mode-enhanced')) {
      trigger.classList.add('bx-mode-enhanced');
      trigger.setAttribute('title', '切换工作流模式 (Build / Plan / Ask)');
    }

    // 注入图片 3 的双向水平箭头矢量图标
    let iconWrap = trigger.querySelector('.bx-mode-badge-wrap');
    if (!iconWrap) {
      iconWrap = document.createElement('span');
      iconWrap.className = 'bx-mode-badge-wrap';
      iconWrap.innerHTML = `<svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5 3.5L2.5 6.5L5.5 9.5"/><path d="M2.5 6.5H13.5"/><path d="M10.5 12.5L13.5 9.5L10.5 6.5"/><path d="M13.5 9.5H2.5"/></svg>`;
      const prefix = trigger.querySelector('.effort-prefix');
      if (prefix) {
        prefix.innerHTML = '';
        prefix.appendChild(iconWrap);
      } else {
        trigger.insertBefore(iconWrap, trigger.firstChild);
      }
    }
  }

  function renderComposerModelPill() {
    const controls = document.querySelector('.model-controls');
    if (!controls) return;

    // 强行隐藏原生冗余的思考强度选择器与宿主选择器
    controls.querySelectorAll('.effort-selector, .model-selector:not(.bx-unified-model-picker)').forEach(el => {
      el.style.setProperty('display', 'none', 'important');
    });

    // 增强模式选择器为图片 3 的双向流线型图标
    enhanceModeSelector();

    let picker = document.getElementById('bx-composer-model-picker');
    if (!picker) {
      picker = document.createElement('div');
      picker.id = 'bx-composer-model-picker';
      picker.className = 'bx-unified-model-picker bx-composer-model-picker';
      picker.innerHTML = `
        <button type="button" class="bx-unified-model-btn bx-composer-model-btn" id="bx-composer-model-btn" title="选择宿主模型与思考强度">
          <span class="bx-host-brand-icon" id="bx-composer-model-icon"></span>
          <span class="bx-unified-model-label" id="bx-composer-model-label">选择模型</span>
          <span class="bx-unified-chevron">▾</span>
        </button>
        <div class="bx-unified-model-popover bx-composer-model-dropdown" id="bx-composer-model-dropdown">
          <!-- Inner Host Drawer (for switching hosts) -->
          <div class="bx-ump-host-drawer" id="bx-ump-host-drawer">
            <div class="bx-ump-drawer-back" id="bx-ump-drawer-back">
              <svg width="14" height="14" viewBox="0 0 20 20" fill="none"><path d="M12.5 15.5L7 10L12.5 4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
              <span>返回模型选择</span>
            </div>
            <div class="bx-ump-drawer-list" id="bx-ump-drawer-list"></div>
          </div>
          <!-- Main Popover View (Matches Image 2) -->
          <div class="bx-ump-main-view" id="bx-ump-main-view">
            <!-- Top Row: Host info -->
            <div class="bx-ump-host-row" id="bx-ump-host-row" title="点击切换宿主工具平台">
              <div class="bx-ump-host-left">
                <span class="bx-host-brand-icon" id="bx-ump-top-host-icon"></span>
                <span class="bx-ump-host-name" id="bx-ump-top-host-name">BigModel</span>
                <span class="bx-ump-host-badge" id="bx-ump-top-host-badge">个人</span>
              </div>
              <span class="bx-ump-host-chevron">
                <svg width="16" height="16" viewBox="0 0 20 20" fill="none"><path d="M7.5 4.5L13 10L7.5 15.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
              </span>
            </div>
            <div class="bx-ump-divider"></div>
            <!-- Middle Section: Models -->
            <div class="bx-ump-section-label">模型</div>
            <div class="bx-ump-model-list" id="bx-ump-model-list"></div>
            <div class="bx-ump-custom-row">
              <input type="text" class="bx-ump-custom-input" id="bx-composer-custom-model-input" placeholder="输入自定义模型 ID (回车应用)..." />
              <button type="button" class="bx-ump-custom-btn" id="bx-composer-custom-model-apply">应用</button>
            </div>
            <div class="bx-ump-divider"></div>
            <!-- Bottom Section: Effort -->
            <div class="bx-ump-effort-header">
              <div class="bx-ump-effort-title-left">
                <span class="bx-ump-effort-label">思考强度</span>
                <span class="bx-ump-effort-val" id="bx-ump-effort-val">最高</span>
              </div>
              <span class="bx-ump-effort-badge" id="bx-ump-effort-badge" title="切换思考强度 (快捷键 Ctrl+T)">^ T</span>
            </div>
            <div class="bx-ump-slider-wrap">
              <div class="bx-ump-slider-track" id="bx-ump-slider-track">
                <div class="bx-ump-slider-particles">
                  <span style="left: 12%; top: 55%; width: 3px; height: 3px; opacity: 0.65;"></span>
                  <span style="left: 24%; top: 30%; width: 2.5px; height: 2.5px; opacity: 0.5;"></span>
                  <span style="left: 29%; top: 40%; width: 3.5px; height: 3.5px; opacity: 0.8;"></span>
                  <span style="left: 42%; top: 60%; width: 2px; height: 2px; opacity: 0.6;"></span>
                  <span style="left: 48%; top: 25%; width: 3px; height: 3px; opacity: 0.75;"></span>
                  <span style="left: 55%; top: 50%; width: 3px; height: 3px; opacity: 0.55;"></span>
                  <span style="left: 63%; top: 35%; width: 2px; height: 2px; opacity: 0.7;"></span>
                  <span style="left: 72%; top: 55%; width: 3px; height: 3px; opacity: 0.85;"></span>
                </div>
                <div class="bx-ump-slider-knob" id="bx-ump-slider-knob"></div>
              </div>
            </div>
          </div>
        </div>
      `;
      controls.insertBefore(picker, controls.firstChild);

      const btn = picker.querySelector('#bx-composer-model-btn');
      const dd = picker.querySelector('#bx-composer-model-dropdown');
      const mainView = picker.querySelector('#bx-ump-main-view');
      const hostDrawer = picker.querySelector('#bx-ump-host-drawer');
      const hostRow = picker.querySelector('#bx-ump-host-row');
      const drawerBack = picker.querySelector('#bx-ump-drawer-back');
      const sliderTrack = picker.querySelector('#bx-ump-slider-track');
      const effortBadge = picker.querySelector('#bx-ump-effort-badge');

      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        picker.classList.toggle('open');
        dd.classList.toggle('open');
        if (dd.classList.contains('open')) {
          positionPopover(dd, btn);
        } else {
          hostDrawer.classList.remove('open');
          mainView.classList.remove('hidden');
        }
      });

      document.addEventListener('mousedown', (e) => {
        if (picker && !picker.contains(e.target)) {
          picker.classList.remove('open');
          dd.classList.remove('open');
          hostDrawer.classList.remove('open');
          mainView.classList.remove('hidden');
        }
      });

      // Host row opens host drawer
      if (hostRow) {
        hostRow.addEventListener('click', (e) => {
          e.stopPropagation();
          mainView.classList.add('hidden');
          hostDrawer.classList.add('open');
          renderHostDrawerList();
          positionPopover(dd, btn);
        });
      }

      // Back button in host drawer
      if (drawerBack) {
        drawerBack.addEventListener('click', (e) => {
          e.stopPropagation();
          hostDrawer.classList.remove('open');
          mainView.classList.remove('hidden');
          positionPopover(dd, btn);
        });
      }

      if (!window._bxPopoverResizeBound) {
        window._bxPopoverResizeBound = true;
        window.addEventListener('resize', () => {
          const ddEl = document.getElementById('bx-composer-model-dropdown');
          const btnEl = document.getElementById('bx-composer-model-btn');
          if (ddEl && ddEl.classList.contains('open') && btnEl) {
            positionPopover(ddEl, btnEl);
          }
        });
      }

      // Slider click / drag
      if (sliderTrack) {
        const handleSliderInput = (e) => {
          const rect = sliderTrack.getBoundingClientRect();
          const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
          let bestIdx = 0;
          let minDiff = 999;
          EFFORT_LEVELS.forEach((lvl, idx) => {
            const diff = Math.abs(ratio - (lvl.percent / 100));
            if (diff < minDiff) {
              minDiff = diff;
              bestIdx = idx;
            }
          });
          applyEffortLevel(bestIdx);
        };
        sliderTrack.addEventListener('click', (e) => {
          e.stopPropagation();
          handleSliderInput(e);
        });
      }

      // Effort badge click cycles
      if (effortBadge) {
        effortBadge.addEventListener('click', (e) => {
          e.stopPropagation();
          const curIdx = getCurrentEffortIndex();
          const nextIdx = (curIdx + 1) % EFFORT_LEVELS.length;
          applyEffortLevel(nextIdx);
        });
      }
    }

    const active = getActiveModelOption(selectedHostProvider);
    const effortIdx = getCurrentEffortIndex();
    const btn = picker.querySelector('#bx-composer-model-btn');
    const dd = picker.querySelector('#bx-composer-model-dropdown');
    if (dd && dd.classList.contains('open') && btn) {
      positionPopover(dd, btn);
    }
    const stateKey = `${selectedHostProvider}::${active.id}::${(active.options || []).length}::${effortIdx}`;
    if (picker.getAttribute('data-state-key') === stateKey) {
      // Just sync effort display if needed
      const knob = document.getElementById('bx-ump-slider-knob');
      const valEl = document.getElementById('bx-ump-effort-val');
      if (knob) knob.style.left = EFFORT_LEVELS[effortIdx].pos;
      if (valEl) valEl.textContent = EFFORT_LEVELS[effortIdx].chinese;
      return;
    }
    picker.setAttribute('data-state-key', stateKey);

    // Update capsule button
    const iconEl = document.getElementById('bx-composer-model-icon');
    const labelEl = document.getElementById('bx-composer-model-label');
    if (iconEl) {
      iconEl.innerHTML = VENDOR_ICON_SVGS[active.vendor] || VENDOR_ICON_SVGS[String(active.vendor).toLowerCase()] || HOST_BRAND_SVGS[selectedHostProvider] || HOST_BRAND_SVGS.codex;
    }
    if (labelEl) {
      labelEl.textContent = active.label || '未探查到模型';
    }

    // Update Top Row in Popover
    const topHostIcon = document.getElementById('bx-ump-top-host-icon');
    const topHostName = document.getElementById('bx-ump-top-host-name');
    const topHostBadge = document.getElementById('bx-ump-top-host-badge');
    const hInfo = HOST_DISPLAY_INFO[selectedHostProvider] || { name: selectedHostProvider.toUpperCase(), badge: '宿主' };
    if (topHostIcon) {
      topHostIcon.innerHTML = HOST_BRAND_SVGS[selectedHostProvider] || HOST_BRAND_SVGS.codex;
    }
    if (topHostName) {
      topHostName.textContent = hInfo.name;
    }
    if (topHostBadge) {
      topHostBadge.textContent = hInfo.badge;
    }

    // Update Model List in Popover
    const modelListEl = document.getElementById('bx-ump-model-list');
    if (modelListEl) {
      const hasOptions = Array.isArray(active.options) && active.options.length > 0;
      if (!hasOptions) {
        modelListEl.innerHTML = `
          <div style="padding: 16px 12px; text-align: center; color: rgba(148,163,184,0.7); font-size: 12px; line-height: 1.6;">
            本地未探查到该宿主的有效模型配置
          </div>
        `;
      } else {
        modelListEl.innerHTML = active.options.map(opt => {
          const isAct = opt.id === active.id;
          let badgeText = opt.badge || '';
          if (!badgeText && (opt.id.toLowerCase().includes('flash') || opt.id.toLowerCase().includes('vision') || opt.id.toLowerCase().includes('vl'))) {
            badgeText = '视觉';
          }
          return `
            <div class="bx-ump-model-item bx-composer-model-option ${isAct ? 'active' : ''}" data-model-id="${opt.id}">
              <div class="bx-ump-model-title-wrap">
                <span class="bx-ump-model-name">${opt.label}</span>
                ${badgeText ? `<span class="bx-ump-model-tag" data-badge="${badgeText}">${badgeText}</span>` : ''}
              </div>
              ${isAct ? `<span class="bx-ump-checkmark">${CHECKMARK_SVG}</span>` : ''}
            </div>
          `;
        }).join('');

        modelListEl.querySelectorAll('.bx-ump-model-item').forEach(item => {
          item.onclick = (ev) => {
            ev.stopPropagation();
            const mId = item.getAttribute('data-model-id');
            selectedHostModels[selectedHostProvider] = mId;
            const dd = document.getElementById('bx-composer-model-dropdown');
            if (dd) dd.classList.remove('open');
            picker.classList.remove('open');
            picker.removeAttribute('data-state-key');
            renderComposerModelPill();
            syncComposerModelPreference();
            showToast(`已切换模型为: ${mId}`);
          };
        });
      }
    }

    // Custom model inputs
    const customInp = document.getElementById('bx-composer-custom-model-input');
    const customBtn = document.getElementById('bx-composer-custom-model-apply');
    const applyCustomModel = () => {
      const val = (customInp && customInp.value || '').trim();
      if (!val) return;
      const entry = composerCatalog.find(c => c.provider === selectedHostProvider);
      if (entry) {
        if (!Array.isArray(entry.model_options)) entry.model_options = [];
        if (!entry.model_options.some(o => o.id === val)) {
          entry.model_options.unshift({ id: val, label: val, vendor: 'auto', badge: '自定义' });
        }
      }
      selectedHostModels[selectedHostProvider] = val;
      const dd = document.getElementById('bx-composer-model-dropdown');
      if (dd) dd.classList.remove('open');
      picker.classList.remove('open');
      picker.removeAttribute('data-state-key');
      renderComposerModelPill();
      syncComposerModelPreference();
      showToast(`已设置自定义模型: ${val}`);
    };
    if (customBtn) customBtn.onclick = (ev) => { ev.stopPropagation(); applyCustomModel(); };
    if (customInp) {
      customInp.onclick = (ev) => ev.stopPropagation();
      customInp.onkeydown = (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          ev.stopPropagation();
          applyCustomModel();
        }
      };
    }

    // Update Effort Section Display
    const knob = document.getElementById('bx-ump-slider-knob');
    const valEl = document.getElementById('bx-ump-effort-val');
    if (knob) knob.style.left = EFFORT_LEVELS[effortIdx].pos;
    if (valEl) valEl.textContent = EFFORT_LEVELS[effortIdx].chinese;
  }

  function renderHostDrawerList() {
    const listEl = document.getElementById('bx-ump-drawer-list');
    if (!listEl) return;
    const availableHosts = ['zcode', 'codex', 'claude', 'cline', 'opencode', 'pi', 'dsh', 'bugx'];
    listEl.innerHTML = availableHosts.map(k => {
      const info = HOST_DISPLAY_INFO[k] || { name: k, badge: '宿主' };
      const isAct = k === selectedHostProvider;
      return `
        <div class="bx-ump-drawer-item ${isAct ? 'active' : ''}" data-host-key="${k}">
          <div class="bx-ump-drawer-left">
            <span class="bx-host-brand-icon">${HOST_BRAND_SVGS[k] || ''}</span>
            <span class="bx-ump-host-name">${info.name}</span>
            <span class="bx-ump-host-badge">${info.badge}</span>
          </div>
          ${isAct ? `<span class="bx-ump-checkmark">${CHECKMARK_SVG}</span>` : ''}
        </div>
      `;
    }).join('');

    listEl.querySelectorAll('.bx-ump-drawer-item').forEach(item => {
      item.onclick = (ev) => {
        ev.stopPropagation();
        const k = item.getAttribute('data-host-key');
        selectHostProvider(k);
        const hostDrawer = document.getElementById('bx-ump-host-drawer');
        const mainView = document.getElementById('bx-ump-main-view');
        if (hostDrawer) hostDrawer.classList.remove('open');
        if (mainView) mainView.classList.remove('hidden');
      };
    });
  }

  let cwdModalActiveTab = 'recent'; // 'recent' | 'browse'

  function enhanceCwdModal() {
    const modal = document.querySelector('.modal-card');
    if (!modal) return;
    const headerTitle = modal.querySelector('.modal-header h3');
    if (!headerTitle || !headerTitle.textContent.includes('切换工作目录')) return;

    modal.classList.add('bx-cwd-enhanced-card');

    // 1. Add Close Button (X) to Header if not present
    let closeBtn = modal.querySelector('.modal-header .bx-cwd-close-btn');
    if (!closeBtn) {
      closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'bx-cwd-close-btn';
      closeBtn.innerHTML = '&times;';
      closeBtn.title = '关闭 (Esc)';
      closeBtn.onclick = (e) => {
        e.stopPropagation();
        const cancelBtn = modal.querySelector('.modal-footer .btn:not(.btn-primary)');
        if (cancelBtn) cancelBtn.click();
        else {
          const overlay = modal.closest('.modal-overlay');
          if (overlay) overlay.click();
        }
      };
      const hdr = modal.querySelector('.modal-header');
      if (hdr) hdr.appendChild(closeBtn);
    }

    // 2. Identify Modal Body and its sections
    const modalBody = modal.querySelector('.modal-body');
    if (!modalBody) return;

    const pathGroup = modalBody.querySelector('.field-group:has(.field-row)') || modalBody.children[0];
    const dirBrowser = modalBody.querySelector('.dir-browser') || modalBody.children[1];
    let recentGroup = null;
    const allGroups = modalBody.querySelectorAll('.field-group');
    if (allGroups.length > 1) {
      recentGroup = allGroups[allGroups.length - 1];
    } else if (allGroups.length === 1 && allGroups[0] !== pathGroup) {
      recentGroup = allGroups[0];
    }

    if (pathGroup) pathGroup.classList.add('bx-path-group');
    if (dirBrowser) dirBrowser.classList.add('bx-dir-browser-enhanced');

    // 3. Inject Top Segmented Control (Tabs) if recent projects exist
    const hasRecent = !!(recentGroup && recentGroup.querySelectorAll('.list-row').length > 0);
    let segContainer = modal.querySelector('.bx-cwd-segmented');
    if (hasRecent && !segContainer) {
      segContainer = document.createElement('div');
      segContainer.className = 'bx-cwd-segmented';
      const count = recentGroup.querySelectorAll('.list-row').length;
      segContainer.innerHTML = `
        <button type="button" class="bx-cwd-seg-btn ${cwdModalActiveTab === 'recent' ? 'active' : ''}" data-tab="recent">
          <span class="bx-seg-icon">${BxIcons.clock(13)}</span> 最近项目 <span class="bx-seg-badge">${count}</span>
        </button>
        <button type="button" class="bx-cwd-seg-btn ${cwdModalActiveTab === 'browse' ? 'active' : ''}" data-tab="browse">
          <span class="bx-seg-icon">${BxIcons.folderOpen(13)}</span> 浏览目录与自由路径
        </button>
      `;
      const header = modal.querySelector('.modal-header');
      if (header && header.nextSibling) {
        modal.insertBefore(segContainer, header.nextSibling);
      } else {
        modalBody.prepend(segContainer);
      }

      segContainer.addEventListener('click', (e) => {
        const btn = e.target.closest('.bx-cwd-seg-btn');
        if (!btn) return;
        const targetTab = btn.dataset.tab;
        if (targetTab) {
          cwdModalActiveTab = targetTab;
          updateTabState();
        }
      });
    }

    function updateTabState() {
      if (!hasRecent) {
        modal.classList.remove('bx-tab-recent');
        modal.classList.add('bx-tab-browse');
        return;
      }
      modal.classList.toggle('bx-tab-recent', cwdModalActiveTab === 'recent');
      modal.classList.toggle('bx-tab-browse', cwdModalActiveTab === 'browse');

      if (segContainer) {
        segContainer.querySelectorAll('.bx-cwd-seg-btn').forEach(btn => {
          btn.classList.toggle('active', btn.dataset.tab === cwdModalActiveTab);
        });
      }
    }
    updateTabState();

    // 4. Enhance Recent Projects Tab
    if (recentGroup) {
      recentGroup.classList.add('bx-recent-group');

      // Inject Search Filter input if not present
      let searchRow = recentGroup.querySelector('.bx-recent-search-row');
      if (!searchRow) {
        searchRow = document.createElement('div');
        searchRow.className = 'bx-recent-search-row';
        searchRow.innerHTML = `
          <div class="bx-search-inner">
            <span class="bx-search-icon">${BxIcons.search(13)}</span>
            <input type="text" class="bx-recent-search-input" placeholder="快速过滤最近工作区 (输入名称或路径)..." autocomplete="off" />
          </div>
        `;
        const label = recentGroup.querySelector('.modal-label');
        if (label) {
          label.style.display = 'none';
          label.after(searchRow);
        } else {
          recentGroup.prepend(searchRow);
        }

        const sInput = searchRow.querySelector('.bx-recent-search-input');
        if (sInput) {
          sInput.oninput = (e) => {
            const q = e.target.value.toLowerCase().trim();
            const rows = recentGroup.querySelectorAll('.list-row');
            let visibleCount = 0;
            rows.forEach(row => {
              const text = row.textContent.toLowerCase();
              const match = !q || text.includes(q);
              row.setAttribute('data-hidden', match ? '0' : '1');
              if (match) visibleCount++;
            });
            let emptyMsg = recentGroup.querySelector('.bx-recent-empty');
            if (visibleCount === 0) {
              if (!emptyMsg) {
                emptyMsg = document.createElement('div');
                emptyMsg.className = 'bx-recent-empty';
                emptyMsg.textContent = '未找到匹配的最近项目';
                recentGroup.appendChild(emptyMsg);
              }
              emptyMsg.style.display = 'block';
            } else if (emptyMsg) {
              emptyMsg.style.display = 'none';
            }
          };
        }
      }

      // Add "Browse other directory" shortcut link at the bottom
      let browseLink = recentGroup.querySelector('.bx-recent-browse-link');
      if (!browseLink) {
        browseLink = document.createElement('div');
        browseLink.className = 'bx-recent-browse-link';
        browseLink.innerHTML = `
          <button type="button" class="bx-link-action-btn">
            <span>${BxIcons.folder(13)}</span> 打开其他新目录或输入绝对路径 &rarr;
          </button>
        `;
        browseLink.querySelector('button').onclick = (e) => {
          e.stopPropagation();
          cwdModalActiveTab = 'browse';
          updateTabState();
          const pathInp = pathGroup ? pathGroup.querySelector('input.menu-input') : null;
          if (pathInp) requestAnimationFrame(() => pathInp.focus());
        };
        recentGroup.appendChild(browseLink);
      }

      // Polish each list-row card
      const listRows = recentGroup.querySelectorAll('.list-row');
      listRows.forEach(row => {
        if (row.dataset.bxPolished === '2') return;
        row.dataset.bxPolished = '2';

        const mono = row.querySelector('.mono');
        if (mono) {
          const fullPath = mono.textContent.trim();
          const segs = fullPath.split('/').filter(Boolean);
          const name = segs[segs.length - 1] || fullPath;
          const displayPath = fullPath.startsWith('/Users/') || fullPath.startsWith('/home/')
            ? '~/' + segs.slice(2).join('/')
            : fullPath;

          mono.innerHTML = `
            <div class="bx-proj-row-content">
              <div class="bx-proj-name">${BxIcons.folder(14)} <strong>${name}</strong></div>
              <div class="bx-proj-path" title="${fullPath}">${displayPath}</div>
            </div>
          `;
        }

        // Double-click row directly switches
        row.ondblclick = (e) => {
          e.stopPropagation();
          row.click();
          setTimeout(() => {
            const confirmBtn = modal.querySelector('.modal-footer .btn-primary');
            if (confirmBtn) confirmBtn.click();
          }, 40);
        };

        // Add prominent quick switch button
        let quickBtn = row.querySelector('.bx-quick-switch-btn');
        if (!quickBtn) {
          quickBtn = document.createElement('button');
          quickBtn.type = 'button';
          quickBtn.className = 'bx-quick-switch-btn';
          quickBtn.innerHTML = '切换至此 &rarr;';
          quickBtn.title = '一键切换到该工作区';
          quickBtn.onclick = (e) => {
            e.stopPropagation();
            row.click();
            setTimeout(() => {
              const confirmBtn = modal.querySelector('.modal-footer .btn-primary');
              if (confirmBtn) confirmBtn.click();
            }, 60);
          };
          const delBtn = row.querySelector('button[title*="移除"]');
          if (delBtn) row.insertBefore(quickBtn, delBtn);
          else row.appendChild(quickBtn);
        }
      });
    }

    // 5. Enhance Browse Tab (Path Input & Directory Browser)
    if (pathGroup) {
      // Quick path pills
      let pillRow = pathGroup.querySelector('.bx-path-pills');
      if (!pillRow) {
        pillRow = document.createElement('div');
        pillRow.className = 'bx-path-pills';
        pillRow.innerHTML = `
          <span class="bx-pill-label">快速定位:</span>
          <button type="button" class="bx-path-pill" data-p="~">${BxIcons.home(12)} 主目录 (~)</button>
          <button type="button" class="bx-path-pill" data-p="~/workspace">${BxIcons.files(12)} 工作区 (workspace)</button>
        `;
        pillRow.addEventListener('click', (e) => {
          const btn = e.target.closest('.bx-path-pill');
          if (!btn) return;
          const p = btn.dataset.p;
          const inp = pathGroup.querySelector('input.menu-input');
          const jumpBtn = pathGroup.querySelector('.field-row .btn-primary');
          if (inp && jumpBtn) {
            inp.value = p;
            inp.dispatchEvent(new Event('input', { bubbles: true }));
            jumpBtn.click();
          }
        });
        const fieldRow = pathGroup.querySelector('.field-row');
        if (fieldRow) fieldRow.after(pillRow);
      }

      const hint = pathGroup.querySelector('.field-hint');
      if (hint) {
        hint.innerHTML = '点击目录树可逐层下钻穿透，点击面包屑回退上级，双击子目录即可进入。';
      }
    }

    // Fix Breadcrumbs double-slash bug
    if (dirBrowser) {
      const breadcrumb = dirBrowser.querySelector('.dir-breadcrumb');
      if (breadcrumb) {
        const firstCrumb = breadcrumb.querySelector('.dir-crumb');
        if (firstCrumb && firstCrumb.textContent.trim() === '/') {
          firstCrumb.innerHTML = `${BxIcons.home(13)} 根目录`;
        }
      }

      // 6. Enhance directory items with quick delete action
      const dirItems = dirBrowser.querySelectorAll('.dir-item');
      dirItems.forEach(item => {
        const iconSpan = item.querySelector('span:first-child');
        if (iconSpan && iconSpan.textContent.includes('📁')) {
          iconSpan.innerHTML = BxIcons.folder(14);
        }
        if (item.dataset.bxDelAttached === '1') return;
        item.dataset.bxDelAttached = '1';

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.className = 'bx-cwd-item-del-btn';
        delBtn.innerHTML = BxIcons.trash(12);
        delBtn.title = '删除此文件或目录';
        delBtn.onclick = async (e) => {
          e.stopPropagation();
          e.preventDefault();
          const itemName = item.textContent.replace(/^[📁📄\s]+/, '').replace(/🗑️$/, '').trim();
          const confirmed = window.confirm(`安全确认：\n\n确定要彻底删除 "${itemName}" 吗？此操作无法恢复！`);
          if (!confirmed) return;
          try {
            let curPath = '';
            const pathInp = modal.querySelector('input.menu-input');
            if (pathInp && pathInp.value) curPath = pathInp.value.trim();
            const targetPath = curPath ? `${curPath}/${itemName}` : itemName;
            const res = await fetch('/fs/delete', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ path: targetPath })
            });
            const data = await res.json();
            if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
            showToast(`已删除: ${itemName}`);
            const jumpBtn = modal.querySelector('.field-row .btn-primary');
            if (jumpBtn) jumpBtn.click();
            else item.remove();
          } catch (err) {
            alert(`删除失败: ${err.message}`);
          }
        };
        item.appendChild(delBtn);
      });

      // Also list files in current directory so users can browse and delete files in CWD modal
      let curPath = '';
      const pathInp = modal.querySelector('input.menu-input');
      if (pathInp && pathInp.value) curPath = pathInp.value.trim();
      const dirList = dirBrowser.querySelector('.dir-list');
      if (dirList && curPath && dirList.dataset.bxFilesLoadedPath !== curPath) {
        dirList.dataset.bxFilesLoadedPath = curPath;
        fetch(`/fs/list?path=${encodeURIComponent(curPath)}`)
          .then(r => r.json())
          .then(data => {
            const files = data.files || [];
            files.forEach(fileName => {
              if (dirList.querySelector(`.dir-item[data-filename="${fileName}"]`)) return;
              const fBtn = document.createElement('button');
              fBtn.type = 'button';
              fBtn.className = 'dir-item file-item bx-file-cwd-item';
              fBtn.setAttribute('data-filename', fileName);
              fBtn.innerHTML = `<span>${BxIcons.file(14)}</span><span>${escapeHtml(fileName)}</span>`;
              const fDel = document.createElement('button');
              fDel.type = 'button';
              fDel.className = 'bx-cwd-item-del-btn';
              fDel.innerHTML = BxIcons.trash(12);
              fDel.title = `删除文件 ${fileName}`;
              fDel.onclick = async (e) => {
                e.stopPropagation();
                e.preventDefault();
                if (!confirm(`安全确认：确定要彻底删除文件 "${fileName}" 吗？此操作无法恢复！`)) return;
                try {
                  const targetPath = curPath ? `${curPath}/${fileName}` : fileName;
                  const res = await fetch('/fs/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ path: targetPath })
                  });
                  const d = await res.json();
                  if (!res.ok || !d.ok) throw new Error(d.error || `HTTP ${res.status}`);
                  showToast(`已删除文件: ${fileName}`);
                  fBtn.remove();
                } catch (err) {
                  alert(`删除失败: ${err.message}`);
                }
              };
              fBtn.appendChild(fDel);
              dirList.appendChild(fBtn);
            });
          })
          .catch(e => console.warn('[CwdFiles]', e));
      }
    }
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function enhanceDropdownMenus() {
    // 1. Keep Skills popovers closed (User requirement: no dropdown for skills)
    const skillsMenu = document.querySelector('.item-menu.skills-menu');
    if (skillsMenu) {
      skillsMenu.remove();
    }

    // 2. Enhance MCP Popover Menu (Direct bridge to MCP Marketplace)
    const mcpMenu = document.querySelector('.item-menu.mcp-menu');
    if (mcpMenu) {
      let footerLink = mcpMenu.querySelector('.bx-menu-footer-link');
      if (!footerLink) {
        footerLink = document.createElement('div');
        footerLink.className = 'bx-menu-footer-link';
        footerLink.innerHTML = `<span style="display:inline-flex; align-items:center; gap:6px;">${BxIcons.market(14)} 打开 MCP 市场与服务配置...</span><span class="bx-link-arrow">&rarr;</span>`;
        footerLink.onclick = (e) => {
          e.stopPropagation();
          mcpMenu.remove();
          openSettingsModal('', 'mcps');
        };
      }
      if (mcpMenu.lastElementChild !== footerLink) {
        mcpMenu.appendChild(footerLink);
      }
    }
  }

  // =========================================================================
  // Skills Center View & Marketplace (Right-side primary view)
  // =========================================================================
  const SKILL_SVGS = {
    search: {
      body: '<circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>'
    },
    close: {
      body: '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>'
    },
    plus: {
      body: '<line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line>'
    },
    sparkles: {
      body: '<path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"></path><path d="M5 3v4"></path><path d="M19 17v4"></path><path d="M3 5h4"></path><path d="M17 19h4"></path>'
    },
    chat: {
      body: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>'
    },
    package: {
      body: '<path d="m16.5 9.4-9-5.19M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path><polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline><line x1="12" y1="22.08" x2="12" y2="12"></line>'
    },
    store: {
      body: '<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"></path><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"></path><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"></path><path d="M2 7h20"></path>'
    },
    globe: {
      body: '<circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>'
    },
    shield: {
      body: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path><path d="m9 12 2 2 4-4"></path>'
    },
    lock: {
      body: '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path>'
    },
    briefcase: {
      body: '<rect x="2" y="7" width="20" height="14" rx="2" ry="2"></rect><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"></path>'
    },
    smartphone: {
      body: '<rect x="5" y="2" width="14" height="20" rx="2" ry="2"></rect><line x1="12" y1="18" x2="12.01" y2="18"></line>'
    },
    server: {
      body: '<rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect><rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect><line x1="6" y1="6" x2="6.01" y2="6"></line><line x1="6" y1="18" x2="6.01" y2="18"></line>'
    },
    zap: {
      body: '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>'
    },
    bot: {
      body: '<rect x="3" y="11" width="18" height="10" rx="2"></rect><circle cx="12" cy="5" r="2"></circle><path d="M12 7v4"></path><line x1="8" y1="16" x2="8.01" y2="16"></line><line x1="16" y1="16" x2="16.01" y2="16"></line>'
    },
    flask: {
      body: '<path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"></path><path d="M8.5 2h7"></path><path d="M7 16h10"></path>'
    },
    rocket: {
      body: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"></path><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"></path><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"></path><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"></path>'
    },
    folder: {
      body: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>'
    },
    book: {
      body: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"></path><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"></path>'
    },
    fileText: {
      body: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline>'
    },
    trash: {
      body: '<polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>'
    },
    git: {
      body: '<path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"></path>'
    },
    star: {
      fill: 'currentColor',
      stroke: 'currentColor',
      strokeWidth: 1,
      body: '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>'
    },
    arrowLeft: {
      body: '<line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline>'
    },
    check: {
      body: '<polyline points="20 6 9 17 4 12"></polyline>'
    },
    checkCircle: {
      body: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>'
    },
    copy: {
      body: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>'
    },
    palette: {
      body: '<circle cx="13.5" cy="6.5" r=".5"></circle><circle cx="17.5" cy="10.5" r=".5"></circle><circle cx="8.5" cy="7.5" r=".5"></circle><circle cx="6.5" cy="12.5" r=".5"></circle><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"></path>'
    },
    terminal: {
      body: '<polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line>'
    },
    code: {
      body: '<polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline>'
    },
    settings: {
      body: '<circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>'
    },
    tag: {
      body: '<path d="M12 2H2v10l9.29 9.29c.94.94 2.48.94 3.42 0l6.58-6.58c.94-.94.94-2.48 0-3.42L12 2Z"></path><path d="M7 7h.01"></path>'
    },
    info: {
      body: '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line>'
    },
    externalLink: {
      body: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line>'
    },
    apple: {
      fill: 'currentColor',
      stroke: 'none',
      body: '<path d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M15.97 6.4c.67-.82 1.13-1.96 1-3.1-.98.04-2.16.66-2.85 1.47-.61.71-1.15 1.87-1.01 2.98 1.1.08 2.2-.53 2.86-1.35z"/>'
    },
    android: {
      fill: 'currentColor',
      stroke: 'none',
      body: '<path d="M17.523 15.341c-.551 0-1-.449-1-1s.449-1 1-1 1 .449 1 1-.449 1-1 1m-11.046 0c-.551 0-1-.449-1-1s.449-1 1-1 1 .449 1 1-.449 1-1 1m11.405-6.02l1.997-3.46a.416.416 0 00-.152-.567.416.416 0 00-.568.152l-2.022 3.503c-1.6-1.056-3.551-1.688-5.637-1.688-2.086 0-4.037.632-5.637 1.688L3.843 5.446a.416.416 0 00-.568-.152.416.416 0 00-.152.567l1.997 3.46C2.267 11.01 1 13.916 1 17.159h22c0-3.243-1.267-6.149-4.118-7.838"/>'
    },
    flutter: {
      fill: 'currentColor',
      stroke: 'none',
      body: '<path d="M14.314 0L2.3 12 6 15.7 21.684 0zm.014 11.072L7.857 17.543 14.328 24H21.7l-6.471-6.457 6.471-6.471z"/>'
    },
    megaphone: {
      body: '<path d="m3 11 18-5v12L3 14v-3z"></path><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"></path>'
    },
    video: {
      body: '<polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>'
    },
    plug: {
      body: '<path d="M12 22v-5"></path><path d="M9 8V2"></path><path d="M15 8V2"></path><path d="M18 8v5a6 6 0 0 1-12 0V8z"></path>'
    },
    database: {
      body: '<ellipse cx="12" cy="5" rx="9" ry="3"></ellipse><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>'
    },
    cpu: {
      body: '<rect x="4" y="4" width="16" height="16" rx="2"></rect><rect x="9" y="9" width="6" height="6"></rect><line x1="9" y1="1" x2="9" y2="4"></line><line x1="15" y1="1" x2="15" y2="4"></line><line x1="9" y1="20" x2="9" y2="23"></line><line x1="15" y1="20" x2="15" y2="23"></line><line x1="20" y1="9" x2="23" y2="9"></line><line x1="20" y1="14" x2="23" y2="14"></line><line x1="1" y1="9" x2="4" y2="9"></line><line x1="1" y1="14" x2="4" y2="14"></line>'
    },
    key: {
      body: '<path d="m21 2-2 2m-1.5 1.5L14 9l-1.5-1.5a3 3 0 0 0-4.24 0l-.76.76a3 3 0 0 0 0 4.24l7.5 7.5a3 3 0 0 0 4.24 0l.76-.76a3 3 0 0 0 0-4.24L18.5 13.5 22 10l-1-1 2-2-2-2-2 2z"></path><circle cx="7.5" cy="16.5" r="1.5"></circle>'
    },
    eye: {
      body: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>'
    },
    eyeOff: {
      body: '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line>'
    },
    alertTriangle: {
      body: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line>'
    },
    power: {
      body: '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"></path><line x1="12" y1="2" x2="12" y2="12"></line>'
    },
    sliders: {
      body: '<line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line><line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line><line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line><line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line><line x1="17" y1="16" x2="23" y2="16"></line>'
    },
    gitPullRequest: {
      body: '<circle cx="18" cy="18" r="3"></circle><circle cx="6" cy="6" r="3"></circle><path d="M13 6h3a2 2 0 0 1 2 2v7"></path><line x1="6" y1="9" x2="6" y2="21"></line>'
    },
    tool: {
      body: '<path d="m14.7 6.3 4 4a1 1 0 0 1 0 1.4l-7 7a2 2 0 0 1-2.8 0L2 11.8a2 2 0 0 1 0-2.8l7-7a1 1 0 0 1 1.4 0l4 4a1 1 0 0 1 0 1.4l-1.4 1.4"></path>'
    },
    slash: {
      body: '<circle cx="12" cy="12" r="10"></circle><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"></line>'
    },
    compass: {
      body: '<circle cx="12" cy="12" r="10"></circle><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"></polygon>'
    }
  };

  function getSvgIcon(iconName, options = {}) {
    const size = options.size || 16;
    const strokeWidth = options.strokeWidth || 2;
    const cls = options.className || '';
    const def = SKILL_SVGS[iconName];
    if (!def) return '';
    const fill = def.fill || 'none';
    const stroke = def.stroke || (fill === 'currentColor' ? 'none' : 'currentColor');
    const sw = def.strokeWidth !== undefined ? def.strokeWidth : strokeWidth;
    return `<svg class="bx-sk-svg ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${def.body}</svg>`;
  }

  function renderMarkdown(md) {
    if (!md) return '<p style="color:var(--app-secondary-foreground, #64748b);">暂无详细文档内容</p>';

    let frontmatterHtml = '';
    let content = md;
    const fmMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n/);
    if (fmMatch) {
      const rawFm = fmMatch[1];
      content = content.slice(fmMatch[0].length);
      const fmPairs = [];
      rawFm.split('\n').forEach(line => {
        const parts = line.split(':');
        if (parts.length >= 2 && !line.startsWith(' ') && !line.startsWith('\t')) {
          const k = parts[0].trim();
          const v = parts.slice(1).join(':').trim().replace(/^["']|["']$/g, '');
          if (k && v) fmPairs.push({ key: k, value: v });
        }
      });
      if (fmPairs.length) {
        frontmatterHtml = `
          <div class="bx-sk-frontmatter-box">
            <div class="bx-sk-fm-title">${getSvgIcon('info', { size: 13 })} 元数据规范 (Frontmatter)</div>
            <div class="bx-sk-fm-grid">
              ${fmPairs.map(p => `
                <div class="bx-sk-fm-item">
                  <span class="bx-sk-fm-key">${escapeHtml(p.key)}:</span>
                  <span class="bx-sk-fm-val">${escapeHtml(p.value)}</span>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      }
    }

    const codeBlocks = [];
    content = content.replace(/```([a-zA-Z0-9_-]*)\r?\n([\s\S]*?)```/g, (match, lang, code) => {
      const id = `__CODE_BLOCK_${codeBlocks.length}__`;
      codeBlocks.push({ lang: lang || 'code', code: code.trim() });
      return id;
    });

    let html = escapeHtml(content);

    // Tables
    html = html.replace(/((?:\|[^\n]+\|\r?\n)+)/g, (match) => {
      const lines = match.trim().split(/\r?\n/).map(l => l.trim()).filter(Boolean);
      if (lines.length < 2) return match;
      if (!lines[1].match(/^\|(?:\s*:?-+:?\s*\|)+$/)) return match;
      const headerCells = lines[0].split('|').slice(1, -1).map(c => c.trim());
      let tableHtml = '<div class="bx-sk-table-wrap"><table class="bx-sk-table"><thead><tr>';
      headerCells.forEach(c => { tableHtml += `<th>${c}</th>`; });
      tableHtml += '</tr></thead><tbody>';
      for (let i = 2; i < lines.length; i++) {
        const cells = lines[i].split('|').slice(1, -1).map(c => c.trim());
        tableHtml += '<tr>';
        cells.forEach(c => { tableHtml += `<td>${c}</td>`; });
        tableHtml += '</tr>';
      }
      tableHtml += '</tbody></table></div>';
      return tableHtml;
    });

    // Blockquotes
    html = html.replace(/^(?:&gt;|>)[ ]?(.*)$/gm, '<blockquote class="bx-sk-quote">$1</blockquote>');

    // Headers
    html = html.replace(/^#### (.*$)/gim, '<h4 class="bx-sk-h4">$1</h4>');
    html = html.replace(/^### (.*$)/gim, '<h3 class="bx-sk-h3">$1</h3>');
    html = html.replace(/^## (.*$)/gim, '<h2 class="bx-sk-h2">$1</h2>');
    html = html.replace(/^# (.*$)/gim, '<h1 class="bx-sk-h1">$1</h1>');

    // Horizontal rule
    html = html.replace(/^---$/gim, '<hr class="bx-sk-hr" />');

    // Inline formatting: Bold, Italic, Inline Code, Links
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    html = html.replace(/`([^`]+)`/g, '<code class="bx-sk-inline-code">$1</code>');
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="bx-sk-link">$1</a>');

    // Lists
    html = html.replace(/^\s*[-*]\s+(.*)$/gim, '<li class="bx-sk-li">$1</li>');
    html = html.replace(/((?:<li class="bx-sk-li">.*<\/li>\r?\n?)+)/g, '<ul class="bx-sk-ul">$1</ul>');

    html = html.replace(/^\s*(\d+)\.\s+(.*)$/gim, '<li class="bx-sk-oli" data-num="$1">$2</li>');
    html = html.replace(/((?:<li class="bx-sk-oli".*<\/li>\r?\n?)+)/g, '<ol class="bx-sk-ol">$1</ol>');

    // Paragraphs
    const blocks = html.split(/\n{2,}/);
    html = blocks.map(block => {
      block = block.trim();
      if (!block) return '';
      if (block.startsWith('<h') || block.startsWith('<ul') || block.startsWith('<ol') || 
          block.startsWith('<blockquote') || block.startsWith('<div') || block.startsWith('<table') ||
          block.startsWith('<hr') || block.startsWith('__CODE_BLOCK_')) {
        return block;
      }
      return `<p class="bx-sk-p">${block.replace(/\n/g, '<br/>')}</p>`;
    }).join('\n');

    // Re-insert code blocks
    codeBlocks.forEach((cb, idx) => {
      const id = `__CODE_BLOCK_${idx}__`;
      const blockHtml = `
        <div class="bx-sk-code-block">
          <div class="bx-sk-code-header">
            <span class="bx-sk-code-lang">${escapeHtml(cb.lang || 'code')}</span>
            <button type="button" class="bx-sk-code-copy-btn" data-code="${encodeURIComponent(cb.code)}" title="复制代码">
              ${getSvgIcon('copy', { size: 12 })} <span>复制代码</span>
            </button>
          </div>
          <pre class="bx-sk-pre"><code>${escapeHtml(cb.code)}</code></pre>
        </div>
      `;
      html = html.replace(id, blockHtml);
    });

    return frontmatterHtml + html;
  }

  function getSkillAttributes(item) {
    const name = (item.name || '').toLowerCase();
    const cat = (item.category || '').toLowerCase();

    let domain = { label: '综合研发效能', icon: 'zap', color: 'blue' };
    if (name.includes('security') || name.includes('audit') || name.includes('cso') || name.includes('bureaucracy') || cat === 'domain-audit' || name === 'bugx') {
      domain = { label: '安全攻防与代码审计', icon: 'shield', color: 'red' };
    } else if (name.includes('android') || name.includes('ios') || name.includes('apple') || name.includes('flutter') || name.includes('swift') || name.includes('mobile')) {
      domain = { label: '移动端与跨端工程', icon: 'smartphone', color: 'purple' };
    } else if (name.includes('backend') || name.includes('go') || name.includes('server') || name.includes('api') || name.includes('db')) {
      domain = { label: '后端服务与系统架构', icon: 'server', color: 'emerald' };
    } else if (name.includes('test') || name.includes('qa') || name.includes('fuzz') || name.includes('benchmark') || name.includes('investigate')) {
      domain = { label: '质量工程与自动化测试', icon: 'flask', color: 'amber' };
    } else if (name.includes('cicd') || name.includes('deploy') || name.includes('ship') || name.includes('canary')) {
      domain = { label: 'DevOps 与交付流水线', icon: 'rocket', color: 'cyan' };
    } else if (name.includes('agent') || name.includes('orca') || name.includes('autoplan') || name.includes('orchestration')) {
      domain = { label: 'AI Agent 编排与协同', icon: 'bot', color: 'indigo' };
    } else if (name.includes('design') || name.includes('ui') || name.includes('ux') || name.includes('color') || name.includes('banner') || name.includes('logo') || name.includes('svg')) {
      domain = { label: 'UI/UX 与高阶视觉设计', icon: 'palette', color: 'pink' };
    } else if (name.includes('ad-') || name.includes('ads') || name.includes('copywriting') || name.includes('marketing')) {
      domain = { label: '增长营销与文案工程', icon: 'megaphone', color: 'orange' };
    } else if (name.includes('doc') || name.includes('diagram') || name.includes('make-pdf')) {
      domain = { label: '文档工程与架构资产', icon: 'fileText', color: 'teal' };
    }

    let scope = { label: '本地全局技能', icon: 'globe', badgeClass: 'bx-badge-green' };
    if (item.scope === 'core') {
      scope = { label: '常驻核心系统', icon: 'lock', badgeClass: 'bx-badge-blue' };
    } else if (item.category === 'project' || item.is_installed_project) {
      scope = { label: '当前工程专属', icon: 'briefcase', badgeClass: 'bx-badge-purple' };
    } else if (item.category === 'domain-audit') {
      scope = { label: '官方专精扩展', icon: 'shield', badgeClass: 'bx-badge-blue' };
    } else if (item.category === 'builtin-extension') {
      scope = { label: '官方内置套件', icon: 'package', badgeClass: 'bx-badge-gray' };
    } else if (item.stars !== undefined || item.repo_url) {
      scope = { label: '云端开源生态', icon: 'store', badgeClass: 'bx-badge-orange' };
    }

    let tags = [];
    if (Array.isArray(item.tags) && item.tags.length) {
      tags = [...item.tags];
    } else {
      if (domain.icon === 'shield') tags.push('安全审计', '漏洞防御', '合规检测');
      else if (domain.icon === 'smartphone') tags.push('移动客户端', '跨端架构', 'Native');
      else if (domain.icon === 'server') tags.push('后端API', '微服务', '分布式');
      else if (domain.icon === 'flask') tags.push('自动化测试', '用例驱动', '回归防卫');
      else if (domain.icon === 'rocket') tags.push('CI/CD', '发布列车', '版本治理');
      else if (domain.icon === 'bot') tags.push('多Agent', '流程编排', '工作树隔离');
      else if (domain.icon === 'palette') tags.push('设计系统', '高阶视觉', '交互规范');
      else if (domain.icon === 'megaphone') tags.push('增长运营', '文案策略', '素材裂变');
      else tags.push('工程效能', '自动化', '开发辅助');
      tags.push('Slash 指令', 'SKILL.md 规范');
    }

    return { domain, scope, tags };
  }

  const SkillsCenter = {
    activeTab: 'local', // 'local' | 'market' | 'online'
    localFilter: 'all', // 'all' | 'core' | 'project' | 'global'
    marketFilter: 'all', // 'all' | 'audit' | 'mobile' | 'backend' | 'tools'
    onlineFilter: 'all', // 'all' | 'security' | 'mobile' | 'qa' | 'devops' | 'agents'
    searchQuery: '',
    items: [],
    onlineItems: [],
    workspace: '',
    globalDir: '',
    projects: [],
    isOpen: false,
    viewMode: 'list', // 'list' | 'detail'
    currentDetailSkill: null,
    currentDetailContent: '',
    currentDetailLoading: false,

    init() {
      const mainCol = document.querySelector('.main-column');
      if (!mainCol) return;
      let container = document.getElementById('bx-skills-view-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'bx-skills-view-container';
        container.className = 'bx-skills-view-container';
        mainCol.appendChild(container);
      }
    },

    async open() {
      if (typeof McpCenter !== 'undefined' && McpCenter.isOpen) {
        McpCenter.close();
      }
      this.init();
      this.isOpen = true;
      this.viewMode = 'list';
      const mainCol = document.querySelector('.main-column');
      if (mainCol) mainCol.classList.add('bx-skills-view-active');
      this.updateSidebarButtonState(true);
      await this.loadData();
      this.render();
    },

    close() {
      this.isOpen = false;
      const mainCol = document.querySelector('.main-column');
      if (mainCol) mainCol.classList.remove('bx-skills-view-active');
      this.updateSidebarButtonState(false);
    },

    toggle() {
      if (this.isOpen) this.close();
      else this.open();
    },

    updateSidebarButtonState(active) {
      const actions = document.querySelectorAll('.sidebar-action');
      actions.forEach(btn => {
        const label = btn.querySelector('.sidebar-action-label');
        if (label && label.textContent.trim() === '技能') {
          if (active) btn.classList.add('bx-skills-active');
          else btn.classList.remove('bx-skills-active');
        }
      });
      const railBtns = document.querySelectorAll('.rail-btn');
      railBtns.forEach(btn => {
        if (btn.title === '技能' || btn.getAttribute('aria-label') === '技能') {
          if (active) btn.classList.add('bx-skills-active');
          else btn.classList.remove('bx-skills-active');
        }
      });
    },

    async loadData() {
      try {
        const res = await fetch(`/api/skills/marketplace?project_hash=${encodeURIComponent(currentHash || '')}`);
        if (res.ok) {
          const data = await res.json();
          this.items = data.items || [];
          this.workspace = data.workspace || '';
          this.globalDir = data.global_dir || '';
        }
      } catch (e) {
        console.warn('[SkillsCenter] fetch skills failed:', e);
      }
      try {
        const oRes = await fetch(`/api/skills/online?project_hash=${encodeURIComponent(currentHash || '')}`);
        if (oRes.ok) {
          const oData = await oRes.json();
          this.onlineItems = oData.items || [];
        }
      } catch (e) {
        console.warn('[SkillsCenter] fetch online skills failed:', e);
      }
      try {
        const pRes = await fetch('/projects');
        if (pRes.ok) {
          this.projects = await pRes.json();
        }
      } catch (e) {
        console.warn('[SkillsCenter] fetch projects failed:', e);
      }
    },

    async openSkillDetail(skillName, skillItem) {
      if (!skillItem) {
        skillItem = (this.items || []).find(it => it.name === skillName) ||
                    (this.onlineItems || []).find(it => it.name === skillName) ||
                    { name: skillName, description: '技能规范文档' };
      }
      this.viewMode = 'detail';
      this.currentDetailSkill = skillItem;
      this.currentDetailContent = '';
      this.currentDetailLoading = true;
      this.render();

      try {
        const res = await fetch(`/api/skills/content?project_hash=${encodeURIComponent(currentHash || '')}&name=${encodeURIComponent(skillName)}`);
        if (res.ok) {
          const data = await res.json();
          this.currentDetailContent = data.content || '# 暂无详细文档说明';
          if (data.file_path && !skillItem.file_path) {
            skillItem.file_path = data.file_path;
          }
        } else {
          this.currentDetailContent = `# 无法加载文档 (HTTP ${res.status})`;
        }
      } catch (e) {
        this.currentDetailContent = `# 加载文档失败: ${e.message}`;
      } finally {
        this.currentDetailLoading = false;
        this.render();
      }
    },

    closeSkillDetail() {
      this.viewMode = 'list';
      this.currentDetailSkill = null;
      this.currentDetailContent = '';
      this.render();
    },

    render() {
      const container = document.getElementById('bx-skills-view-container');
      if (!container) return;

      if (this.viewMode === 'detail' && this.currentDetailSkill) {
        this.renderSkillDetail(container);
        return;
      }

      const q = (this.searchQuery || '').toLowerCase().trim();
      const allItems = this.items || [];

      // Local items: core + project + global installed
      const localAll = allItems.filter(it => it.scope === 'core' || it.category === 'project' || it.category === 'global' || it.is_installed_project);
      const marketAll = allItems;
      const onlineAll = this.onlineItems || [];

      let filteredLocal = localAll.filter(it => {
        if (this.localFilter === 'core' && it.scope !== 'core') return false;
        if (this.localFilter === 'project' && it.category !== 'project' && !it.is_installed_project) return false;
        if (this.localFilter === 'global' && it.category !== 'global') return false;
        if (q) {
          const text = (it.name + ' ' + (it.description || '') + ' ' + (it.category || '')).toLowerCase();
          return text.includes(q);
        }
        return true;
      });

      let filteredMarket = marketAll.filter(it => {
        if (this.marketFilter === 'audit' && it.category !== 'domain-audit' && it.scope !== 'core') return false;
        if (this.marketFilter === 'mobile' && !it.name.includes('mobile') && !it.name.includes('android') && !it.name.includes('ios') && !it.name.includes('flutter') && !it.name.includes('apple') && !it.name.includes('swift')) return false;
        if (this.marketFilter === 'backend' && !it.name.includes('backend') && !it.name.includes('go') && !it.name.includes('native') && !it.name.includes('data') && !it.name.includes('web')) return false;
        if (this.marketFilter === 'tools' && it.category !== 'builtin-extension' && !it.name.includes('doc') && !it.name.includes('test') && !it.name.includes('fix') && !it.name.includes('review')) return false;
        if (q) {
          const text = (it.name + ' ' + (it.description || '') + ' ' + (it.category || '')).toLowerCase();
          return text.includes(q);
        }
        return true;
      });

      let filteredOnline = onlineAll.filter(it => {
        if (this.onlineFilter === 'security' && it.category !== 'security') return false;
        if (this.onlineFilter === 'mobile' && it.category !== 'mobile') return false;
        if (this.onlineFilter === 'qa' && it.category !== 'qa') return false;
        if (this.onlineFilter === 'devops' && it.category !== 'devops') return false;
        if (this.onlineFilter === 'agents' && it.category !== 'agents') return false;
        if (q) {
          const text = (it.name + ' ' + (it.title || '') + ' ' + (it.description || '') + ' ' + (it.author || '') + ' ' + (it.tags || []).join(' ')).toLowerCase();
          return text.includes(q);
        }
        return true;
      });

      container.innerHTML = `
        <div class="bx-skview-header">
          <div class="bx-skview-title-wrap">
            <div class="bx-skview-badge-pill">${getSvgIcon('sparkles', { size: 13 })} 技能工作台</div>
            <h2 class="bx-skview-title">技能全景管理与生态市场</h2>
            <p class="bx-skview-desc">统一管理当前工程与本地全局技能包，浏览云端精选开源技能，支持从 Git 仓库或 URL 一键部署。</p>
          </div>
          <div class="bx-skview-actions">
            <div class="bx-skview-search-box">
              <span class="bx-skview-search-icon">${getSvgIcon('search', { size: 14 })}</span>
              <input type="text" id="bx-skview-search" class="bx-skview-search-input" placeholder="搜索技能名称、指令 (/audit)、作者或标签..." value="${escapeHtml(this.searchQuery)}" />
              ${this.searchQuery ? `<button type="button" id="bx-skview-clear-search" class="bx-skview-search-clear" title="清空搜索">${getSvgIcon('close', { size: 12 })}</button>` : ''}
            </div>
            <button type="button" id="bx-skview-install-remote-btn" class="bx-action-btn secondary" title="通过 Git 仓库或远程 URL 在线安装任意技能">
              <span>${getSvgIcon('plus', { size: 14 })}</span><span>从 Git / 远程 URL 安装</span>
            </button>
            <button type="button" id="bx-skview-automatch-btn" class="bx-action-btn primary" title="基于当前工程技术栈自动匹配安装适用技能">
              <span>${getSvgIcon('sparkles', { size: 14 })}</span><span>AI 智能匹配</span>
            </button>
            <button type="button" id="bx-skview-back-chat-btn" class="bx-action-btn secondary" title="切回当前对话窗口">
              <span>${getSvgIcon('chat', { size: 14 })}</span><span>返回对话</span>
            </button>
          </div>
        </div>

        <div class="bx-skview-tabs-bar">
          <div class="bx-skview-tabs">
            <button type="button" class="bx-skview-tab ${this.activeTab === 'local' ? 'active' : ''}" data-tab="local">
              <span>${getSvgIcon('package', { size: 14 })}</span>
              <span>本地已安装技能</span>
              <span class="bx-skview-tab-count">${localAll.length}</span>
            </button>
            <button type="button" class="bx-skview-tab ${this.activeTab === 'market' ? 'active' : ''}" data-tab="market">
              <span>${getSvgIcon('store', { size: 14 })}</span>
              <span>内置专精包</span>
              <span class="bx-skview-tab-count">${marketAll.length}</span>
            </button>
            <button type="button" class="bx-skview-tab ${this.activeTab === 'online' ? 'active' : ''}" data-tab="online">
              <span>${getSvgIcon('globe', { size: 14 })}</span>
              <span>云端在线市场</span>
              <span class="bx-skview-tab-count">${onlineAll.length}</span>
            </button>
          </div>
          <div class="bx-skview-subfilters">
            ${this.renderSubFilters()}
          </div>
        </div>

        <div class="bx-skview-body">
          ${this.activeTab === 'local' ? this.renderLocalSkills(filteredLocal) :
            this.activeTab === 'market' ? this.renderMarketSkills(filteredMarket) :
            this.renderOnlineSkills(filteredOnline)}
        </div>
      `;

      this.bindEvents(container);
    },

    renderSkillDetail(container) {
      const it = this.currentDetailSkill;
      if (!it) {
        this.viewMode = 'list';
        this.render();
        return;
      }

      const { domain, scope, tags } = getSkillAttributes(it);
      const iconSvg = this.getSkillIcon(it.name, it.category, 26);
      const isCore = it.scope === 'core';
      const isProject = it.category === 'project' || it.is_installed_project;
      const isGlobal = it.category === 'global' || it.is_installed_global;
      const rawContent = this.currentDetailContent || '';
      const renderedDoc = renderMarkdown(rawContent);
      const displayPath = it.file_path || it.path || '内置组件 / 待安装';

      container.innerHTML = `
        <div class="bx-sk-detail-view">
          <!-- Top Navigation & Breadcrumbs -->
          <div class="bx-sk-detail-top-bar">
            <div class="bx-sk-detail-nav-left">
              <button type="button" class="bx-sk-detail-back-btn" id="bx-sk-detail-back-btn" title="返回技能列表">
                ${getSvgIcon('arrowLeft', { size: 14 })}
                <span>返回技能列表</span>
              </button>
              <div class="bx-sk-detail-breadcrumbs">
                <span>技能工作台</span>
                <span>/</span>
                <span>${this.activeTab === 'local' ? '本地技能' : this.activeTab === 'market' ? '内置专精' : '云端市场'}</span>
                <span>/</span>
                <span class="active">/${escapeHtml(it.name)}</span>
              </div>
            </div>
            <div class="bx-sk-detail-nav-actions">
              <button type="button" class="bx-action-btn secondary btn-sm" id="bx-sk-detail-copy-cmd" title="复制指令到剪贴板">
                ${getSvgIcon('copy', { size: 13 })}
                <span>复制指令</span>
              </button>
              <button type="button" class="bx-action-btn primary btn-sm" id="bx-sk-detail-use-chat" title="填入当前对话框">
                ${getSvgIcon('chat', { size: 13 })}
                <span>填入当前对话</span>
              </button>
            </div>
          </div>

          <!-- Hero Header -->
          <div class="bx-sk-detail-hero">
            <div class="bx-sk-detail-avatar">
              ${iconSvg}
            </div>
            <div class="bx-sk-detail-title-col">
              <div class="bx-sk-detail-name-row">
                <h1 class="bx-sk-detail-name">/${escapeHtml(it.name)}</h1>
                <span class="bx-badge ${scope.badgeClass}">${getSvgIcon(scope.icon, { size: 12 })} ${scope.label}</span>
                <span class="bx-badge bx-badge-blue">${getSvgIcon(domain.icon, { size: 12 })} ${domain.label}</span>
                ${it.enabled !== false ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 12 })} 运行就绪</span>` : `<span class="bx-badge bx-badge-gray">已停用</span>`}
                ${it.version ? `<span class="bx-badge bx-badge-gray">v${escapeHtml(it.version)}</span>` : ''}
                ${it.stars !== undefined ? `<span class="bx-sk-star-badge">${getSvgIcon('star', { size: 12 })} ${it.stars}</span>` : ''}
              </div>
              <p class="bx-sk-detail-desc">${escapeHtml(it.description || '暂无描述信息')}</p>
              <div class="bx-sk-detail-meta-row">
                <span class="bx-sk-meta-item" title="${escapeHtml(displayPath)}">
                  ${getSvgIcon('folder', { size: 13 })}
                  <span>${escapeHtml(displayPath)}</span>
                </span>
                ${it.author ? `
                  <span class="bx-sk-meta-item">
                    ${getSvgIcon('bot', { size: 13 })}
                    <span>作者: ${escapeHtml(it.author)}</span>
                  </span>
                ` : ''}
              </div>
            </div>
          </div>

          <!-- Main Content Grid -->
          <div class="bx-sk-detail-content-grid">
            <!-- Left: Documentation Reader -->
            <div class="bx-sk-detail-main">
              <div class="bx-sk-doc-card">
                <div class="bx-sk-doc-card-header">
                  <div class="bx-sk-doc-header-title">
                    ${getSvgIcon('book', { size: 16 })}
                    <span>SKILL.md 规范与使用指南</span>
                  </div>
                  <div class="bx-sk-doc-header-actions">
                    <button type="button" class="bx-action-btn secondary btn-sm" id="bx-sk-doc-copy-raw">
                      ${getSvgIcon('copy', { size: 12 })}
                      <span>复制 Markdown 源码</span>
                    </button>
                  </div>
                </div>
                <div class="bx-sk-markdown-body" id="bx-sk-markdown-body">
                  ${this.currentDetailLoading ? `
                    <div style="text-align:center; padding: 60px 20px; color: var(--app-secondary-foreground, #64748b);">
                      <div style="font-size: 1.8rem; margin-bottom: 12px; display: inline-block;">
                        ${getSvgIcon('sparkles', { size: 32 })}
                      </div>
                      <div>正在加载技能文档...</div>
                    </div>
                  ` : renderedDoc}
                </div>
              </div>
            </div>

            <!-- Right: Attribute Classification & Settings -->
            <div class="bx-sk-detail-sidebar">
              <!-- Card 1: 属性与特征档案 -->
              <div class="bx-sk-sidebar-card bx-sk-prop-card">
                <div class="bx-sk-sidebar-card-title">
                  ${getSvgIcon('tag', { size: 16 })}
                  <span>属性归类与特征档案</span>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">业务领域 (Domain)</div>
                  <div class="bx-sk-prop-value" style="display:flex; align-items:center; gap:6px;">
                    ${getSvgIcon(domain.icon, { size: 14 })}
                    <span style="font-weight:600;">${domain.label}</span>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">生态作用域 (Scope)</div>
                  <div class="bx-sk-prop-value" style="display:flex; align-items:center; gap:6px;">
                    ${getSvgIcon(scope.icon, { size: 14 })}
                    <span>${scope.label}</span>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">触发指令 (Trigger)</div>
                  <div class="bx-sk-prop-value">
                    <code class="bx-sk-cmd-pill" id="bx-sk-cmd-click-copy" title="点击复制指令">/${escapeHtml(it.name)}</code>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">物理存储路径 (Location)</div>
                  <div class="bx-sk-path-box" id="bx-sk-path-copy" title="点击复制完整路径">
                    <span>${escapeHtml(displayPath)}</span>
                    ${getSvgIcon('copy', { size: 12 })}
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">能力与场景标签 (Capabilities)</div>
                  <div class="bx-sk-tag-list" style="margin-top:6px;">
                    ${tags.map(t => `<span class="bx-sk-tag-pill">${escapeHtml(t)}</span>`).join('')}
                  </div>
                </div>
              </div>

              <!-- Card 2: 技能设置与控制 -->
              <div class="bx-sk-sidebar-card bx-sk-settings-card">
                <div class="bx-sk-sidebar-card-title">
                  ${getSvgIcon('settings', { size: 16 })}
                  <span>技能控制与流转设置</span>
                </div>
                
                <!-- Setting: Enable / Disable -->
                <div class="bx-sk-prop-item">
                  <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                      <div style="font-size:0.82rem; font-weight:700;">技能激活状态</div>
                      <div style="font-size:0.72rem; color:var(--app-secondary-foreground, #64748b); margin-top:2px;">
                        ${isCore ? '核心系统组件常驻生效' : (it.enabled !== false ? '当前在对话会话中已激活' : '当前已停用，不响应会话')}
                      </div>
                    </div>
                    <div>
                      ${!isCore ? `
                        <label class="bx-switch" title="切换技能启用状态">
                          <input type="checkbox" id="bx-sk-detail-toggle-cb" ${it.enabled !== false ? 'checked' : ''} />
                          <span class="bx-slider"></span>
                        </label>
                      ` : `<span class="bx-badge bx-badge-blue">常驻核心</span>`}
                    </div>
                  </div>
                </div>

                <!-- Setting: Lifecycle / Scope Transfer -->
                <div class="bx-sk-prop-item" style="margin-top:16px; padding-top:16px; border-top:1px solid var(--border, rgba(148,163,184,0.12));">
                  <div style="font-size:0.82rem; font-weight:700; margin-bottom:8px;">作用域与项目流转</div>
                  <div style="display:flex; flex-direction:column; gap:8px;">
                    ${!isCore && isGlobal && !isProject ? `
                      <button type="button" class="bx-action-btn primary w-full" id="bx-sk-detail-mount-btn">
                        ${getSvgIcon('rocket', { size: 14 })}
                        <span>挂载到当前项目 (.agents/skills)</span>
                      </button>
                    ` : ''}
                    ${!isCore && isProject ? `
                      <button type="button" class="bx-action-btn secondary w-full" id="bx-sk-detail-promote-btn">
                        ${getSvgIcon('globe', { size: 14 })}
                        <span>提升为本地全局技能</span>
                      </button>
                      <button type="button" class="bx-action-btn danger w-full" id="bx-sk-detail-unmount-btn">
                        ${getSvgIcon('trash', { size: 14 })}
                        <span>从当前项目卸载</span>
                      </button>
                    ` : ''}
                    ${!isCore && !isGlobal && !isProject ? `
                      <button type="button" class="bx-action-btn primary w-full" id="bx-sk-detail-install-proj-btn">
                        ${getSvgIcon('briefcase', { size: 14 })}
                        <span>安装到当前工程...</span>
                      </button>
                      <button type="button" class="bx-action-btn secondary w-full" id="bx-sk-detail-install-glob-btn">
                        ${getSvgIcon('globe', { size: 14 })}
                        <span>安装到本地全局</span>
                      </button>
                    ` : ''}
                    <button type="button" class="bx-action-btn secondary w-full" id="bx-sk-detail-open-chat-btn">
                      ${getSvgIcon('chat', { size: 14 })}
                      <span>一键填入对话框 (/${escapeHtml(it.name)})</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      `;

      this.bindDetailEvents(container, it);
    },

    bindDetailEvents(container, it) {
      const backBtn = container.querySelector('#bx-sk-detail-back-btn');
      if (backBtn) {
        backBtn.onclick = () => this.closeSkillDetail();
      }

      const copyCmdBtn = container.querySelector('#bx-sk-detail-copy-cmd');
      if (copyCmdBtn) {
        copyCmdBtn.onclick = () => {
          navigator.clipboard.writeText(`/${it.name}`);
          showToast(`已复制指令: /${it.name}`);
        };
      }

      const clickCopyCmd = container.querySelector('#bx-sk-cmd-click-copy');
      if (clickCopyCmd) {
        clickCopyCmd.onclick = () => {
          navigator.clipboard.writeText(`/${it.name}`);
          showToast(`已复制指令: /${it.name}`);
        };
      }

      const pathCopy = container.querySelector('#bx-sk-path-copy');
      if (pathCopy) {
        pathCopy.onclick = () => {
          const path = it.file_path || it.path || '';
          if (path) {
            navigator.clipboard.writeText(path);
            showToast('已复制完整文件路径');
          }
        };
      }

      const useChatBtn = container.querySelector('#bx-sk-detail-use-chat');
      const openChatBtn = container.querySelector('#bx-sk-detail-open-chat-btn');
      const doFillChat = () => {
        this.close();
        const textarea = document.querySelector('textarea, input[type="text"].chat-input');
        if (textarea) {
          textarea.value = `/${it.name} `;
          textarea.focus();
          showToast(`已填入技能指令: /${it.name}`);
        }
      };
      if (useChatBtn) useChatBtn.onclick = doFillChat;
      if (openChatBtn) openChatBtn.onclick = doFillChat;

      const copyRawBtn = container.querySelector('#bx-sk-doc-copy-raw');
      if (copyRawBtn) {
        copyRawBtn.onclick = () => {
          navigator.clipboard.writeText(this.currentDetailContent || '');
          showToast('已复制 Markdown 规范源码');
        };
      }

      container.querySelectorAll('.bx-sk-code-copy-btn').forEach(btn => {
        btn.onclick = () => {
          const code = decodeURIComponent(btn.dataset.code || '');
          navigator.clipboard.writeText(code);
          showToast('已复制代码块');
        };
      });

      const toggleCb = container.querySelector('#bx-sk-detail-toggle-cb');
      if (toggleCb) {
        toggleCb.onchange = async () => {
          const enabled = toggleCb.checked;
          try {
            const res = await fetch('/api/skills/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, name: it.name, enabled, mount: true })
            });
            if (res.ok) {
              it.enabled = enabled;
              showToast(`${enabled ? '已启用' : '已停用'} 技能: /${it.name}`);
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('切换技能状态失败: ' + e.message);
          }
        };
      }

      const mountBtn = container.querySelector('#bx-sk-detail-mount-btn');
      if (mountBtn) {
        mountBtn.onclick = async () => {
          try {
            const res = await fetch('/api/skills/install', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: it.name, target: 'project', project_hash: currentHash })
            });
            if (res.ok) {
              showToast(`技能 /${it.name} 已成功挂载到当前项目！`);
              it.is_installed_project = true;
              it.category = 'project';
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('挂载失败: ' + e.message);
          }
        };
      }

      const unmountBtn = container.querySelector('#bx-sk-detail-unmount-btn');
      if (unmountBtn) {
        unmountBtn.onclick = async () => {
          if (!confirm(`确定要从当前工程卸载技能 /${it.name} 吗？`)) return;
          try {
            const res = await fetch('/api/skills/uninstall', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: it.name, target: 'project', project_hash: currentHash })
            });
            if (res.ok) {
              showToast(`已从当前工程卸载技能: /${it.name}`);
              it.is_installed_project = false;
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('卸载失败');
          }
        };
      }

      const installGlobBtn = container.querySelector('#bx-sk-detail-install-glob-btn');
      if (installGlobBtn) {
        installGlobBtn.onclick = async () => {
          try {
            const res = await fetch('/api/skills/install', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: it.name, target: 'global' })
            });
            if (res.ok) {
              showToast(`技能 /${it.name} 已安装到本地全局目录！`);
              it.is_installed_global = true;
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('全局安装失败: ' + e.message);
          }
        };
      }

      const installProjBtn = container.querySelector('#bx-sk-detail-install-proj-btn');
      if (installProjBtn) {
        installProjBtn.onclick = () => {
          this.openInstallProjectModal(it.name);
        };
      }

      const promoteBtn = container.querySelector('#bx-sk-detail-promote-btn');
      if (promoteBtn) {
        promoteBtn.onclick = async () => {
          try {
            const res = await fetch('/api/skills/install', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: it.name, target: 'global' })
            });
            if (res.ok) {
              showToast(`技能 /${it.name} 已同步安装到本地全局目录！`);
              it.is_installed_global = true;
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('操作失败: ' + e.message);
          }
        };
      }
    },

    renderSubFilters() {
      if (this.activeTab === 'local') {
        return `
          <button type="button" class="bx-subfilter-btn ${this.localFilter === 'all' ? 'active' : ''}" data-filter="all">全部已安装</button>
          <button type="button" class="bx-subfilter-btn ${this.localFilter === 'core' ? 'active' : ''}" data-filter="core">${getSvgIcon('lock', { size: 13 })} 核心安全审计</button>
          <button type="button" class="bx-subfilter-btn ${this.localFilter === 'project' ? 'active' : ''}" data-filter="project">${getSvgIcon('briefcase', { size: 13 })} 本项目专属</button>
          <button type="button" class="bx-subfilter-btn ${this.localFilter === 'global' ? 'active' : ''}" data-filter="global">${getSvgIcon('globe', { size: 13 })} 本地全局技能</button>
        `;
      } else if (this.activeTab === 'market') {
        return `
          <button type="button" class="bx-subfilter-btn ${this.marketFilter === 'all' ? 'active' : ''}" data-filter="all">全部专精</button>
          <button type="button" class="bx-subfilter-btn ${this.marketFilter === 'audit' ? 'active' : ''}" data-filter="audit">${getSvgIcon('shield', { size: 13 })} 官方攻防审计包</button>
          <button type="button" class="bx-subfilter-btn ${this.marketFilter === 'mobile' ? 'active' : ''}" data-filter="mobile">${getSvgIcon('smartphone', { size: 13 })} 移动端与客户端</button>
          <button type="button" class="bx-subfilter-btn ${this.marketFilter === 'backend' ? 'active' : ''}" data-filter="backend">${getSvgIcon('server', { size: 13 })} 后端与系统</button>
          <button type="button" class="bx-subfilter-btn ${this.marketFilter === 'tools' ? 'active' : ''}" data-filter="tools">${getSvgIcon('zap', { size: 13 })} 研发效能与文档</button>
        `;
      } else {
        return `
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === 'all' ? 'active' : ''}" data-filter="all">全部在线</button>
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === 'security' ? 'active' : ''}" data-filter="security">${getSvgIcon('shield', { size: 13 })} 安全渗透与审计</button>
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === 'mobile' ? 'active' : ''}" data-filter="mobile">${getSvgIcon('smartphone', { size: 13 })} 移动端与全栈</button>
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === 'qa' ? 'active' : ''}" data-filter="qa">${getSvgIcon('flask', { size: 13 })} 自动化测试与质量</button>
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === 'devops' ? 'active' : ''}" data-filter="devops">${getSvgIcon('rocket', { size: 13 })} DevOps 与 CI/CD</button>
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === 'agents' ? 'active' : ''}" data-filter="agents">${getSvgIcon('bot', { size: 13 })} AI Agent 治理</button>
        `;
      }
    },

    getSkillIcon(name, category, size = 20) {
      const n = (name || '').toLowerCase();
      const c = (category || '').toLowerCase();
      if (n.includes('git') || n.includes('hub')) return getSvgIcon('git', { size });
      if (n.includes('android')) return getSvgIcon('android', { size });
      if (n.includes('apple') || n.includes('ios') || n.includes('swift')) return getSvgIcon('apple', { size });
      if (n.includes('flutter') || n.includes('dart')) return getSvgIcon('flutter', { size });
      if (n.includes('test') || n.includes('qa') || n.includes('fuzz')) return getSvgIcon('flask', { size });
      if (n.includes('design') || n.includes('ui') || n.includes('color') || n.includes('banner') || n.includes('logo') || n.includes('svg')) return getSvgIcon('palette', { size });
      if (n.includes('security') || n.includes('cso') || n.includes('audit') || n.includes('bureaucracy') || c === 'domain-audit' || n === 'bugx') return getSvgIcon('shield', { size });
      if (n.includes('ad-') || n.includes('ads') || n.includes('copywriting') || n.includes('marketing')) return getSvgIcon('megaphone', { size });
      if (n.includes('browse') || n.includes('chrome') || n.includes('web') || n.includes('scrape')) return getSvgIcon('globe', { size });
      if (n.includes('video') || n.includes('film') || n.includes('lottie')) return getSvgIcon('video', { size });
      if (n.includes('doc') || n.includes('pdf')) return getSvgIcon('fileText', { size });
      if (n.includes('cicd') || n.includes('deploy') || n.includes('ship')) return getSvgIcon('rocket', { size });
      if (n.includes('orca') || n.includes('agent') || n.includes('autoplan') || n.includes('orchestration')) return getSvgIcon('bot', { size });
      if (n.includes('go-') || n.includes('golang') || n.includes('backend') || n.includes('server')) return getSvgIcon('server', { size });
      if (n.includes('code') || n.includes('dev')) return getSvgIcon('terminal', { size });
      return getSvgIcon('zap', { size });
    },

    renderLocalSkills(items) {
      if (!items.length) {
        return `
          <div style="text-align:center; padding: 60px 20px; color: var(--app-secondary-foreground, #64748b);">
            <div style="margin-bottom: 12px; display:inline-block; color:var(--app-secondary-foreground, #94a3b8);">${getSvgIcon('package', { size: 48 })}</div>
            <div style="font-weight: 600; font-size: 1rem; margin-bottom: 4px; color: var(--app-primary-foreground, #0f172a);">未找到匹配的本地技能</div>
            <div style="font-size: 0.8rem; opacity: 0.75;">您可以切换筛选条件，或在「技能生态市场」中一键安装技能到本地或项目。</div>
          </div>
        `;
      }
      return `
        <div class="bx-sk-grid">
          ${items.map(it => {
            const isCore = it.scope === 'core';
            const isProject = it.category === 'project' || it.is_installed_project;
            const badgeClass = isCore ? 'bx-badge-blue' : isProject ? 'bx-badge-purple' : 'bx-badge-green';
            const badgeIcon = isCore ? 'lock' : isProject ? 'briefcase' : 'globe';
            const badgeLabel = isCore ? '核心系统' : isProject ? '项目专属' : '本地全局';
            const displayPath = (it.file_path || '').replace(/^.*?((\.agents|\/skills|\.gemini).*)$/, '');
            const icon = this.getSkillIcon(it.name, it.category, 20);

            return `
              <div class="bx-sk-card ${it.enabled ? '' : 'disabled'}" data-name="${escapeHtml(it.name)}">
                <div class="bx-sk-card-head" style="cursor:pointer;">
                  <div class="bx-sk-avatar" title="/${escapeHtml(it.name)}">${icon}</div>
                  <div class="bx-sk-title-col">
                    <div class="bx-sk-title-row">
                      <span class="bx-sk-name" title="/${escapeHtml(it.name)}">/${escapeHtml(it.name)}</span>
                      <span class="bx-badge ${badgeClass}">${getSvgIcon(badgeIcon, { size: 11 })} ${badgeLabel}</span>
                    </div>
                  </div>
                  <div class="bx-sk-toggle-wrap">
                    ${!isCore ? `
                      <label class="bx-switch" title="${it.enabled ? '点击停用该技能' : '点击启用该技能'}">
                        <input type="checkbox" class="bx-sk-toggle-cb" data-name="${it.name}" ${it.enabled ? 'checked' : ''} />
                        <span class="bx-slider"></span>
                      </label>
                    ` : `<span class="bx-badge bx-badge-blue" style="font-size:10px;">${getSvgIcon('lock', { size: 10 })} 常驻核心</span>`}
                  </div>
                </div>
                <div class="bx-sk-card-body" style="cursor:pointer;">
                  <p class="bx-sk-desc" title="${escapeHtml(it.description || '')}">${escapeHtml(it.description || '暂无描述')}</p>
                  <div class="bx-sk-meta" title="${escapeHtml(it.file_path || '')}">
                    <span>${getSvgIcon('folder', { size: 12 })}</span><span>${escapeHtml(displayPath || it.file_path || '')}</span>
                  </div>
                </div>
                <div class="bx-sk-card-footer">
                  <button type="button" class="bx-sk-action-btn primary bx-sk-use-btn" data-cmd="/${it.name}">
                    <span>${getSvgIcon('chat', { size: 13 })}</span><span>填入对话</span>
                  </button>
                  <button type="button" class="bx-sk-action-btn secondary bx-sk-view-btn" data-name="${it.name}">
                    <span>${getSvgIcon('book', { size: 13 })}</span><span>查看文档</span>
                  </button>
                  ${!isCore && !isProject ? `
                    <button type="button" class="bx-sk-action-btn secondary bx-sk-mount-proj-btn" data-name="${it.name}" title="挂载至当前工程">
                      <span>${getSvgIcon('rocket', { size: 13 })}</span><span>挂载项目</span>
                    </button>
                  ` : ''}
                  ${!isCore && isProject ? `
                    <button type="button" class="bx-sk-action-btn danger bx-sk-unmount-proj-btn" data-name="${it.name}" title="从当前项目卸载">
                      <span>${getSvgIcon('trash', { size: 13 })}</span><span>卸载</span>
                    </button>
                  ` : ''}
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    },

    renderMarketSkills(items) {
      if (!items.length) {
        return `
          <div style="text-align:center; padding: 60px 20px; color: var(--app-secondary-foreground, #64748b);">
            <div style="margin-bottom: 12px; display:inline-block; color:var(--app-secondary-foreground, #94a3b8);">${getSvgIcon('store', { size: 48 })}</div>
            <div style="font-weight: 600; font-size: 1rem; margin-bottom: 4px; color: var(--app-primary-foreground, #0f172a);">未找到匹配的市场技能包</div>
            <div style="font-size: 0.8rem; opacity: 0.75;">请尝试清空搜索关键字或切换分类标签。</div>
          </div>
        `;
      }
      return `
        <div class="bx-sk-grid">
          ${items.map(it => {
            const isInstalledGlobal = Boolean(it.is_installed_global);
            const isInstalledProject = Boolean(it.is_installed_project);
            const displayPath = (it.file_path || '').replace(/^.*?((\.agents|\/skills|\.gemini).*)$/, '');
            const icon = this.getSkillIcon(it.name, it.category, 20);

            return `
              <div class="bx-sk-card" data-name="${escapeHtml(it.name)}">
                <div class="bx-sk-card-head" style="cursor:pointer;">
                  <div class="bx-sk-avatar" title="/${escapeHtml(it.name)}">${icon}</div>
                  <div class="bx-sk-title-col">
                    <div class="bx-sk-title-row">
                      <span class="bx-sk-name" title="/${escapeHtml(it.name)}">/${escapeHtml(it.name)}</span>
                      <span class="bx-badge ${it.category === 'domain-audit' ? 'bx-badge-blue' : 'bx-badge-gray'}">
                        ${getSvgIcon(it.category === 'domain-audit' ? 'shield' : 'package', { size: 11 })}
                        ${it.category === 'domain-audit' ? '官方专精包' : '官方扩展'}
                      </span>
                    </div>
                  </div>
                  <div style="display:flex; gap:6px; align-items:center; flex-shrink:0;">
                    ${isInstalledGlobal ? `<span class="bx-badge bx-badge-green" style="font-size:10px;">${getSvgIcon('check', { size: 10 })} 全局已装</span>` : ''}
                    ${isInstalledProject ? `<span class="bx-badge bx-badge-blue" style="font-size:10px;">${getSvgIcon('check', { size: 10 })} 项目已装</span>` : ''}
                  </div>
                </div>
                <div class="bx-sk-card-body" style="cursor:pointer;">
                  <p class="bx-sk-desc" title="${escapeHtml(it.description || '')}">${escapeHtml(it.description || '暂无描述')}</p>
                  <div class="bx-sk-meta" title="${escapeHtml(it.file_path || it.category || '')}">
                    <span>${getSvgIcon('package', { size: 12 })}</span><span>${escapeHtml(displayPath || it.file_path || it.category || '')}</span>
                  </div>
                </div>
                <div class="bx-sk-card-footer">
                  <button type="button" class="bx-sk-action-btn ${isInstalledGlobal ? 'secondary' : 'primary'} bx-sk-install-global-btn" data-name="${it.name}">
                    <span>${getSvgIcon('globe', { size: 13 })}</span><span>${isInstalledGlobal ? '重装到全局' : '安装到全局'}</span>
                  </button>
                  <button type="button" class="bx-sk-action-btn primary bx-sk-install-project-btn" data-name="${it.name}">
                    <span>${getSvgIcon('briefcase', { size: 13 })}</span><span>安装到指定项目...</span>
                  </button>
                  <button type="button" class="bx-sk-action-btn secondary bx-sk-view-btn" data-name="${it.name}">
                    <span>${getSvgIcon('book', { size: 13 })}</span><span>查看文档</span>
                  </button>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    },

    renderOnlineSkills(items) {
      if (!items.length) {
        return `
          <div style="text-align:center; padding: 60px 20px; color: var(--app-secondary-foreground, #64748b);">
            <div style="margin-bottom: 12px; display:inline-block; color:var(--app-secondary-foreground, #94a3b8);">${getSvgIcon('globe', { size: 48 })}</div>
            <div style="font-weight: 600; font-size: 1rem; margin-bottom: 4px; color: var(--app-primary-foreground, #0f172a);">未找到匹配的云端在线技能</div>
            <div style="font-size: 0.8rem; opacity: 0.75;">您可以点击右上角「+ 从 Git / 远程 URL 安装」直接安装任意第三方技能。</div>
          </div>
        `;
      }
      return `
        <div class="bx-sk-grid">
          ${items.map(it => {
            const isInstalledGlobal = Boolean(it.is_installed_global);
            const isInstalledProject = Boolean(it.is_installed_project);
            const icon = this.getSkillIcon(it.name, it.category, 20);

            return `
              <div class="bx-sk-card" data-name="${escapeHtml(it.name)}">
                <div class="bx-sk-card-head" style="cursor:pointer;">
                  <div class="bx-sk-avatar" title="/${escapeHtml(it.name)}">${icon}</div>
                  <div class="bx-sk-title-col">
                    <div class="bx-sk-title-row">
                      <span class="bx-sk-name" title="/${escapeHtml(it.name)}">/${escapeHtml(it.name)}</span>
                      <span class="bx-badge bx-badge-purple" style="font-size:10px;">by ${escapeHtml(it.author || 'community')}</span>
                      <span class="bx-badge bx-badge-gray" style="font-size:10px;">v${escapeHtml(it.version || '1.0')}</span>
                    </div>
                  </div>
                  <div style="display:flex; gap:6px; align-items:center; flex-shrink:0;">
                    <span class="bx-sk-star-badge" title="GitHub / Gitea 收藏关注数">${getSvgIcon('star', { size: 11 })} ${it.stars || 0}</span>
                    ${isInstalledGlobal ? `<span class="bx-badge bx-badge-green" style="font-size:10px;">${getSvgIcon('check', { size: 10 })} 全局</span>` : ''}
                    ${isInstalledProject ? `<span class="bx-badge bx-badge-blue" style="font-size:10px;">${getSvgIcon('check', { size: 10 })} 项目</span>` : ''}
                  </div>
                </div>
                <div class="bx-sk-card-body" style="cursor:pointer;">
                  <p class="bx-sk-desc" title="${escapeHtml(it.description || '')}">${escapeHtml(it.description || '暂无描述')}</p>
                  <div class="bx-sk-meta" title="${escapeHtml(it.repo_url || '')}">
                    <span>${getSvgIcon('git', { size: 12 })}</span><span>${escapeHtml(it.repo_url || '')}</span>
                  </div>
                </div>
                <div class="bx-sk-card-footer">
                  <button type="button" class="bx-sk-action-btn ${isInstalledGlobal ? 'secondary' : 'primary'} bx-sk-online-install-global-btn" data-name="${it.name}" data-url="${it.repo_url}">
                    <span>${getSvgIcon('globe', { size: 13 })}</span><span>${isInstalledGlobal ? '重装到全局' : '安装到全局'}</span>
                  </button>
                  <button type="button" class="bx-sk-action-btn primary bx-sk-online-install-proj-btn" data-name="${it.name}" data-url="${it.repo_url}">
                    <span>${getSvgIcon('briefcase', { size: 13 })}</span><span>安装到指定项目...</span>
                  </button>
                  <button type="button" class="bx-sk-action-btn secondary bx-sk-view-btn" data-name="${it.name}">
                    <span>${getSvgIcon('book', { size: 13 })}</span><span>查看文档</span>
                  </button>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    },

    bindEvents(container) {
      // Tab switching
      container.querySelectorAll('.bx-skview-tab').forEach(tabBtn => {
        tabBtn.onclick = () => {
          this.activeTab = tabBtn.dataset.tab;
          this.render();
        };
      });

      // Subfilter switching
      container.querySelectorAll('.bx-subfilter-btn').forEach(btn => {
        btn.onclick = () => {
          if (this.activeTab === 'local') {
            this.localFilter = btn.dataset.filter;
          } else if (this.activeTab === 'market') {
            this.marketFilter = btn.dataset.filter;
          } else {
            this.onlineFilter = btn.dataset.filter;
          }
          this.render();
        };
      });

      // Search input
      const searchInp = container.querySelector('#bx-skview-search');
      if (searchInp) {
        searchInp.oninput = (e) => {
          this.searchQuery = e.target.value;
          this.render();
          const newInp = container.querySelector('#bx-skview-search');
          if (newInp) {
            newInp.focus();
            newInp.setSelectionRange(newInp.value.length, newInp.value.length);
          }
        };
      }
      const clearBtn = container.querySelector('#bx-skview-clear-search');
      if (clearBtn) {
        clearBtn.onclick = () => {
          this.searchQuery = '';
          this.render();
        };
      }

      // Back to chat
      const backBtn = container.querySelector('#bx-skview-back-chat-btn');
      if (backBtn) {
        backBtn.onclick = () => this.close();
      }

      // AI Match
      const autoMatchBtn = container.querySelector('#bx-skview-automatch-btn');
      if (autoMatchBtn) {
        autoMatchBtn.onclick = async () => {
          try {
            autoMatchBtn.disabled = true;
            autoMatchBtn.innerHTML = `<span>${getSvgIcon('sparkles', { size: 14 })}</span><span>匹配中...</span>`;
            const res = await fetch('/api/capabilities/auto-match', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, apply: true })
            });
            if (res.ok) {
              const data = await res.json();
              showToast(`AI 自动挂载匹配项: ${(data.applied_skills || []).join(', ') || '已是最优配置'}`);
              await this.loadData();
              this.render();
            }
          } catch (e) {
            console.warn('[AutoMatch] failed:', e);
            showToast('智能匹配失败');
          } finally {
            autoMatchBtn.disabled = false;
            autoMatchBtn.innerHTML = `<span>${getSvgIcon('sparkles', { size: 14 })}</span><span>AI 智能匹配</span>`;
          }
        };
      }

      // Card click to open detail
      container.querySelectorAll('.bx-sk-card').forEach(card => {
        const name = card.dataset.name;
        const head = card.querySelector('.bx-sk-card-head');
        const body = card.querySelector('.bx-sk-card-body');
        if (head) {
          head.onclick = (e) => {
            if (e.target.closest('.bx-switch, .bx-sk-toggle-cb')) return;
            this.openSkillDetail(name);
          };
        }
        if (body) {
          body.onclick = () => this.openSkillDetail(name);
        }
      });

      // Card actions: Use in chat
      container.querySelectorAll('.bx-sk-use-btn').forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const cmd = btn.dataset.cmd;
          this.close();
          const textarea = document.querySelector('textarea, input[type="text"].chat-input');
          if (textarea) {
            textarea.value = cmd + ' ';
            textarea.focus();
            showToast(`已填入技能指令: ${cmd}`);
          }
        };
      });

      // Card actions: Toggle skill enabled
      container.querySelectorAll('.bx-sk-toggle-cb').forEach(cb => {
        cb.onclick = (e) => e.stopPropagation();
        cb.onchange = async (e) => {
          e.stopPropagation();
          const name = cb.dataset.name;
          const enabled = cb.checked;
          try {
            const res = await fetch('/api/skills/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, name, enabled, mount: true })
            });
            if (res.ok) {
              showToast(`${enabled ? '已启用' : '已停用'} 技能: /${name}`);
              await this.loadData();
              this.render();
            }
          } catch (e) {
            console.warn('[ToggleSkill] failed:', e);
            showToast('切换技能状态失败');
          }
        };
      });

      // Card actions: View detail
      container.querySelectorAll('.bx-sk-view-btn').forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          this.openSkillDetail(btn.dataset.name);
        };
      });

      // Card actions: Mount to project
      container.querySelectorAll('.bx-sk-mount-proj-btn').forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const name = btn.dataset.name;
          try {
            const res = await fetch('/api/skills/install', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, target: 'project', project_hash: currentHash })
            });
            if (res.ok) {
              showToast(`技能 /${name} 已成功挂载到当前项目！`);
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('挂载技能失败');
          }
        };
      });

      // Card actions: Unmount from project
      container.querySelectorAll('.bx-sk-unmount-proj-btn').forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const name = btn.dataset.name;
          if (!confirm(`确定要从当前工程卸载技能 /${name} 吗？`)) return;
          try {
            const res = await fetch('/api/skills/uninstall', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, target: 'project', project_hash: currentHash })
            });
            if (res.ok) {
              showToast(`已从当前工程卸载技能: /${name}`);
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('卸载失败');
          }
        };
      });

      // Card actions: Install to global
      container.querySelectorAll('.bx-sk-install-global-btn').forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const name = btn.dataset.name;
          try {
            btn.disabled = true;
            btn.innerHTML = `<span>${getSvgIcon('globe', { size: 13 })}</span><span>安装中...</span>`;
            const res = await fetch('/api/skills/install', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name, target: 'global' })
            });
            if (res.ok) {
              showToast(`技能 /${name} 已成功安装到本地全局 (~/.agents/skills)！`);
              await this.loadData();
              this.render();
            }
          } catch (e) {
            showToast('全局安装失败');
          } finally {
            btn.disabled = false;
          }
        };
      });

      // Card actions: Install to designated project
      container.querySelectorAll('.bx-sk-install-project-btn').forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const name = btn.dataset.name;
          this.openInstallProjectModal(name);
        };
      });

      // Remote Install from Header
      const remoteInstallBtn = container.querySelector('#bx-skview-install-remote-btn');
      if (remoteInstallBtn) {
        remoteInstallBtn.onclick = () => this.openRemoteInstallModal();
      }

      // Online card actions: Install to global
      container.querySelectorAll('.bx-sk-online-install-global-btn').forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const name = btn.dataset.name;
          const url = btn.dataset.url;
          try {
            btn.disabled = true;
            btn.innerHTML = `<span>${getSvgIcon('globe', { size: 13 })}</span><span>拉取安装中...</span>`;
            const res = await fetch('/api/skills/install-remote', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ url, name, target: 'global', project_hash: currentHash })
            });
            const d = await res.json();
            if (res.ok && d.ok) {
              showToast(`技能 /${name} 已成功安装到本地全局！`);
              await this.loadData();
              this.render();
            } else {
              showToast(`安装失败: ${d.error || '未知错误'}`);
            }
          } catch (e) {
            showToast('全局安装失败: ' + e.message);
          } finally {
            btn.disabled = false;
          }
        };
      });

      // Online card actions: Install to designated project
      container.querySelectorAll('.bx-sk-online-install-proj-btn').forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const name = btn.dataset.name;
          const url = btn.dataset.url;
          this.openRemoteInstallModal(url, name);
        };
      });
    },

    openInstallProjectModal(skillName) {
      let modal = document.getElementById('bx-sk-proj-modal-overlay');
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'bx-sk-proj-modal-overlay';
        modal.className = 'bx-sk-modal-overlay';
        document.body.appendChild(modal);
      }

      const curWs = this.workspace || '当前工作区';
      const curWsName = curWs.split('/').filter(Boolean).pop() || '当前工作区';

      modal.innerHTML = `
        <div class="bx-sk-modal-card">
          <div class="bx-sk-modal-header">
            <div class="bx-sk-modal-title" style="display:flex; align-items:center; gap:8px;">
              ${getSvgIcon('briefcase', { size: 18 })}
              <span>安装技能到指定项目</span>
            </div>
            <button type="button" class="bx-modal-close" id="bx-sk-proj-close">${getSvgIcon('close', { size: 14 })}</button>
          </div>
          <div class="bx-sk-modal-body">
            <div style="margin-bottom: 14px;">
              <label style="display:block; font-size:0.78rem; font-weight:600; margin-bottom:6px;">目标技能</label>
              <div style="display:inline-flex; align-items:center; gap:6px; background:rgba(2,132,199,0.12); color:#0284c7; padding:4px 12px; border-radius:6px; font-weight:700; font-family:monospace;">
                ${getSvgIcon('zap', { size: 13 })} /${escapeHtml(skillName)}
              </div>
            </div>

            <div style="margin-bottom: 14px;">
              <label style="display:block; font-size:0.78rem; font-weight:600; margin-bottom:6px;">选择目标工程项目</label>
              <select id="bx-sk-proj-select" style="width:100%; height:36px; border-radius:8px; border:1px solid rgba(148,163,184,0.3); background:var(--app-surface-background, #f8fafc); color:inherit; padding:0 10px; font-size:0.82rem; outline:none;">
                <option value="${escapeHtml(curWs)}">当前工作区: ${escapeHtml(curWsName)} (${escapeHtml(curWs)})</option>
                ${(this.projects || []).map(p => {
                  const pPath = p.working_dir || '';
                  if (!pPath || pPath === curWs) return '';
                  return `<option value="${escapeHtml(pPath)}">${escapeHtml(p.name || pPath)} (${escapeHtml(pPath)})</option>`;
                }).join('')}
                <option value="__custom__">自定义其他工程绝对路径...</option>
              </select>
            </div>

            <div id="bx-sk-custom-path-group" style="display:none; margin-bottom: 14px;">
              <label style="display:block; font-size:0.78rem; font-weight:600; margin-bottom:6px;">输入工程绝对路径</label>
              <input type="text" id="bx-sk-custom-path-inp" placeholder="/Users/name/workspace/my-app" style="width:100%; height:36px; border-radius:8px; border:1px solid rgba(148,163,184,0.3); background:var(--app-surface-background, #f8fafc); color:inherit; padding:0 10px; font-size:0.82rem; outline:none;" />
            </div>

            <div style="font-size:0.74rem; color:var(--app-secondary-foreground, #64748b); line-height:1.5;">
              安装后将把该技能部署至目标项目的 <code>.agents/skills/${escapeHtml(skillName)}</code> 目录，并在该项目配置中自动激活。
            </div>
          </div>
          <div class="bx-sk-modal-footer">
            <button type="button" class="bx-action-btn secondary" id="bx-sk-proj-cancel">取消</button>
            <button type="button" class="bx-action-btn primary" id="bx-sk-proj-confirm">立即安装并启用</button>
          </div>
        </div>
      `;

      modal.style.display = 'flex';
      const sel = modal.querySelector('#bx-sk-proj-select');
      const customGroup = modal.querySelector('#bx-sk-custom-path-group');
      const customInp = modal.querySelector('#bx-sk-custom-path-inp');

      sel.onchange = () => {
        if (sel.value === '__custom__') {
          customGroup.style.display = 'block';
          if (customInp) customInp.focus();
        } else {
          customGroup.style.display = 'none';
        }
      };

      const closeModal = () => { modal.style.display = 'none'; };
      modal.querySelector('#bx-sk-proj-close').onclick = closeModal;
      modal.querySelector('#bx-sk-proj-cancel').onclick = closeModal;
      modal.onclick = (e) => { if (e.target === modal) closeModal(); };

      modal.querySelector('#bx-sk-proj-confirm').onclick = async () => {
        let targetPath = sel.value;
        if (targetPath === '__custom__') {
          targetPath = (customInp && customInp.value || '').trim();
          if (!targetPath) {
            alert('请输入目标工程绝对路径');
            return;
          }
        }
        try {
          const res = await fetch('/api/skills/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: skillName, target: 'project', target_path: targetPath })
          });
          const result = await res.json();
          if (res.ok && result.ok) {
            showToast(result.message || `成功安装技能 /${skillName} 到指定项目！`);
            closeModal();
            await this.loadData();
            this.render();
          } else {
            showToast(`安装失败: ${result.error || '未知错误'}`);
          }
        } catch (e) {
          showToast(`安装发生错误: ${e.message}`);
        }
      };
    },

    openRemoteInstallModal(defaultUrl = '', defaultName = '') {
      let modal = document.getElementById('bx-sk-remote-modal-overlay');
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'bx-sk-remote-modal-overlay';
        modal.className = 'bx-sk-modal-overlay';
        document.body.appendChild(modal);
      }

      const curWs = this.workspace || '当前工作区';
      const curWsName = curWs.split('/').filter(Boolean).pop() || '当前工作区';

      modal.innerHTML = `
        <div class="bx-sk-modal-card">
          <div class="bx-sk-modal-header">
            <div>
              <div class="bx-sk-modal-title" style="display:flex; align-items:center; gap:8px;">
                ${getSvgIcon('plus', { size: 18 })}
                <span>从 Git 仓库或远程 URL 在线安装技能</span>
              </div>
              <div style="font-size:0.74rem; color:var(--app-secondary-foreground, #64748b); margin-top:2px;">
                支持 GitHub、Gitea、GitLab 仓库地址，或直接指定包含 <code>SKILL.md</code> 的远程 URL。
              </div>
            </div>
            <button type="button" class="bx-modal-close" id="bx-sk-remote-close">${getSvgIcon('close', { size: 14 })}</button>
          </div>
          <div class="bx-sk-modal-body">
            <div style="margin-bottom: 14px;">
              <label style="display:block; font-size:0.78rem; font-weight:600; margin-bottom:6px;">远程源地址 (Git 仓库或 SKILL.md 地址) <span style="color:#ef4444;">*</span></label>
              <input type="text" id="bx-sk-remote-url-inp" placeholder="https://github.com/owner/skill-repo.git 或 https://.../SKILL.md" value="${escapeHtml(defaultUrl)}" style="width:100%; height:36px; border-radius:8px; border:1px solid rgba(148,163,184,0.3); background:var(--app-surface-background, #f8fafc); color:inherit; padding:0 10px; font-size:0.82rem; outline:none;" />
              <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:6px; font-size:0.7rem; align-items:center;">
                <span style="color:var(--app-secondary-foreground, #64748b);">热门预设:</span>
                <button type="button" class="bx-sk-preset-btn" data-url="https://github.com/bugx-community/web-security-hunter.git" data-name="web-security-hunter">
                  ${getSvgIcon('shield', { size: 12 })} OWASP 渗透
                </button>
                <button type="button" class="bx-sk-preset-btn" data-url="https://github.com/bugx-community/playwright-agent-qa.git" data-name="playwright-agent-qa">
                  ${getSvgIcon('flask', { size: 12 })} Playwright QA
                </button>
                <button type="button" class="bx-sk-preset-btn" data-url="https://github.com/bugx-community/zero-slip-dynamics.git" data-name="zero-slip-dynamics">
                  ${getSvgIcon('bot', { size: 12 })} 双轨守门员
                </button>
              </div>
            </div>

            <div style="margin-bottom: 14px;">
              <label style="display:block; font-size:0.78rem; font-weight:600; margin-bottom:6px;">自定义技能名称 (可选)</label>
              <input type="text" id="bx-sk-remote-name-inp" placeholder="留空则自动根据仓库名或 SKILL.md 识别" value="${escapeHtml(defaultName)}" style="width:100%; height:36px; border-radius:8px; border:1px solid rgba(148,163,184,0.3); background:var(--app-surface-background, #f8fafc); color:inherit; padding:0 10px; font-size:0.82rem; outline:none;" />
            </div>

            <div style="margin-bottom: 14px;">
              <label style="display:block; font-size:0.78rem; font-weight:600; margin-bottom:6px;">安装目标</label>
              <select id="bx-sk-remote-target-select" style="width:100%; height:36px; border-radius:8px; border:1px solid rgba(148,163,184,0.3); background:var(--app-surface-background, #f8fafc); color:inherit; padding:0 10px; font-size:0.82rem; outline:none;">
                <option value="project" data-path="${escapeHtml(curWs)}">当前工作区: ${escapeHtml(curWsName)} (${escapeHtml(curWs)})</option>
                <option value="global">本地全局目录 (~/.agents/skills)</option>
                ${(this.projects || []).map(p => {
                  const pPath = p.working_dir || '';
                  if (!pPath || pPath === curWs) return '';
                  return `<option value="project" data-path="${escapeHtml(pPath)}">${escapeHtml(p.name || pPath)} (${escapeHtml(pPath)})</option>`;
                }).join('')}
              </select>
            </div>

            <div style="font-size:0.74rem; color:var(--app-secondary-foreground, #64748b); line-height:1.5;">
              系统将深度克隆（--depth 1）或拉取该源的 <code>SKILL.md</code>，验证指令契约无误后部署到目标位置并自动激活。
            </div>
          </div>
          <div class="bx-sk-modal-footer">
            <button type="button" class="bx-action-btn secondary" id="bx-sk-remote-cancel">取消</button>
            <button type="button" class="bx-action-btn primary" id="bx-sk-remote-confirm">立即拉取并安装</button>
          </div>
        </div>
      `;

      modal.style.display = 'flex';
      const urlInp = modal.querySelector('#bx-sk-remote-url-inp');
      const nameInp = modal.querySelector('#bx-sk-remote-name-inp');
      const targetSel = modal.querySelector('#bx-sk-remote-target-select');
      const confirmBtn = modal.querySelector('#bx-sk-remote-confirm');
      const closeBtn = modal.querySelector('#bx-sk-remote-close');
      const cancelBtn = modal.querySelector('#bx-sk-remote-cancel');

      modal.querySelectorAll('.bx-sk-preset-btn').forEach(pBtn => {
        pBtn.onclick = () => {
          urlInp.value = pBtn.dataset.url;
          nameInp.value = pBtn.dataset.name;
        };
      });

      const closeModal = () => { modal.style.display = 'none'; };
      closeBtn.onclick = closeModal;
      cancelBtn.onclick = closeModal;
      modal.onclick = (e) => { if (e.target === modal) closeModal(); };

      confirmBtn.onclick = async () => {
        const url = urlInp.value.trim();
        const skName = nameInp.value.trim();
        if (!url) {
          showToast('请输入远程源地址');
          urlInp.focus();
          return;
        }

        const selOpt = targetSel.options[targetSel.selectedIndex];
        const target = selOpt.value;
        const targetPath = selOpt.getAttribute('data-path');

        try {
          confirmBtn.disabled = true;
          confirmBtn.innerHTML = `<span>${getSvgIcon('sparkles', { size: 14 })}</span><span>拉取并安装中...</span>`;
          const res = await fetch('/api/skills/install-remote', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              url,
              name: skName,
              target,
              target_path: targetPath,
              project_hash: currentHash
            })
          });
          const d = await res.json();
          if (res.ok && d.ok) {
            showToast(`${d.message || '安装成功！'}`);
            closeModal();
            await this.loadData();
            this.render();
          } else {
            showToast(`安装失败: ${d.error || '未知错误'}`);
          }
        } catch (e) {
          showToast('远程安装失败: ' + e.message);
        } finally {
          confirmBtn.disabled = false;
          confirmBtn.innerHTML = '立即拉取并安装';
        }
      };
    }
  };

  const MCP_DESCRIPTIONS = {
    'bugx-supervisor': 'BugX 核心审计调度控制面，提供任务认领、证据链校验、心跳续约与结算门禁',
    'bugx-supervisor-worker': 'BugX Worker 受限执行视图，专为并发子智能体提供零逃逸任务认领与上报',
    'bugx-docs': '项目级 SQLite FTS5 全文文档检索引擎，支持架构规范与安全基线毫秒级检索',
    'postmcp': 'HTTP/REST/GraphQL 动态接口契约测试与越权差分发包探针',
    'playwright': '真实无头浏览器自动化探针，用于 DOM XSS、鉴权流、CSRF 与前端视觉回归验证',
    'dbhub': '多源关系型数据库（PostgreSQL/MySQL/SQLite）Schema 审计与只读安全查询探针',
    'redis': 'Redis 缓存键值状态审计、TTL 策略核验与分布式锁竞争探针',
    'semgrep': '多语言 AST 静态污点规则扫描器，快速定位高危危险汇（Sinks）',
    'mobile': 'Android/iOS 移动端组件暴露面、Intent 劫持与 UI 自动化探针',
    'desktop': 'macOS/Windows/Linux 原生桌面应用窗口树检查与交互探针',
    'native': 'C/C++/Rust 原生二进制符号分析、ASan 内存错误与 Fuzzing 探针'
  };

  const McpCenter = {
    activeTab: 'local', // 'local' | 'market' | 'online'
    localFilter: 'all', // 'all' | 'active' | 'disabled' | 'needs_creds' | 'builtin'
    marketFilter: 'all', // 'all' | 'core' | 'storage' | 'testing' | 'security' | 'mobile'
    onlineFilter: 'all', // 'all' | 'devops' | 'storage' | 'tools' | 'search' | 'security'
    searchQuery: '',
    mountedServers: [],
    marketplaceItems: [],
    onlineItems: [],
    isOpen: false,
    viewMode: 'list', // 'list' | 'detail'
    currentDetailMcp: null,
    currentDetailContent: '',
    currentDetailLoading: false,
    currentDetailTools: [],
    credentialInputs: {},
    toolsFilterQuery: '',

    init() {
      const mainCol = document.querySelector('.main-column');
      if (!mainCol) return;
      let container = document.getElementById('bx-mcp-view-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'bx-mcp-view-container';
        container.className = 'bx-mcp-view-container';
        mainCol.appendChild(container);
      }
    },

    async open() {
      if (typeof SkillsCenter !== 'undefined' && SkillsCenter.isOpen) {
        SkillsCenter.close();
      }
      this.init();
      this.isOpen = true;
      this.viewMode = 'list';
      const mainCol = document.querySelector('.main-column');
      if (mainCol) mainCol.classList.add('bx-mcp-view-active');
      this.updateSidebarButtonState(true);
      await this.loadData();
      this.render();
    },

    close() {
      this.isOpen = false;
      const mainCol = document.querySelector('.main-column');
      if (mainCol) mainCol.classList.remove('bx-mcp-view-active');
      this.updateSidebarButtonState(false);
    },

    toggle() {
      if (this.isOpen) this.close();
      else this.open();
    },

    updateSidebarButtonState(active) {
      const actions = document.querySelectorAll('.sidebar-action');
      actions.forEach(btn => {
        const label = btn.querySelector('.sidebar-action-label');
        if (label && label.textContent.trim() === 'MCP') {
          if (active) btn.classList.add('bx-mcp-active');
          else btn.classList.remove('bx-mcp-active');
        }
      });
      const railBtns = document.querySelectorAll('.rail-btn');
      railBtns.forEach(btn => {
        if (btn.title === 'MCP' || btn.getAttribute('aria-label') === 'MCP') {
          if (active) btn.classList.add('bx-mcp-active');
          else btn.classList.remove('bx-mcp-active');
        }
      });
    },

    async loadData() {
      try {
        const res = await fetch(`/mcp/status?project_hash=${encodeURIComponent(currentHash || '')}`);
        if (res.ok) {
          const data = await res.json();
          this.mountedServers = data.servers || [];
        }
      } catch (e) {
        console.warn('[McpCenter] fetch /mcp/status failed:', e);
      }
      try {
        const mRes = await fetch(`/api/mcp/marketplace?project_hash=${encodeURIComponent(currentHash || '')}`);
        if (mRes.ok) {
          const mData = await mRes.json();
          this.marketplaceItems = mData.items || [];
        }
      } catch (e) {
        console.warn('[McpCenter] fetch /api/mcp/marketplace failed:', e);
      }
      try {
        const oRes = await fetch(`/api/mcp/online?project_hash=${encodeURIComponent(currentHash || '')}`);
        if (oRes.ok) {
          const oData = await oRes.json();
          this.onlineItems = oData.items || [];
        }
      } catch (e) {
        console.warn('[McpCenter] fetch /api/mcp/online failed:', e);
      }
    },

    async openMcpDetail(serverName, serverItem) {
      if (!serverItem) {
        serverItem = (this.mountedServers || []).find(s => s.name === serverName) ||
                     (this.marketplaceItems || []).find(m => m.id === serverName || m.name === serverName) ||
                     (this.onlineItems || []).find(o => o.id === serverName || o.name === serverName) ||
                     { name: serverName, description: 'MCP 服务规范' };
      }
      this.viewMode = 'detail';
      this.currentDetailMcp = serverItem;
      this.currentDetailContent = '';
      this.currentDetailLoading = true;
      this.currentDetailTools = [];
      this.credentialInputs = {};
      this.toolsFilterQuery = '';
      this.render();

      try {
        const res = await fetch(`/api/mcp/content?project_hash=${encodeURIComponent(currentHash || '')}&name=${encodeURIComponent(serverName)}`);
        if (res.ok) {
          const data = await res.json();
          this.currentDetailContent = data.content || '# 暂无详细规范说明';
          this.currentDetailTools = data.tools || [];
          Object.assign(serverItem, data);
        } else {
          this.currentDetailContent = `# 无法加载 MCP 规范说明 (HTTP ${res.status})`;
        }
      } catch (e) {
        this.currentDetailContent = `# 加载 MCP 规范失败: ${e.message}`;
      } finally {
        this.currentDetailLoading = false;
        this.render();
      }
    },

    closeMcpDetail() {
      this.viewMode = 'list';
      this.currentDetailMcp = null;
      this.currentDetailContent = '';
      this.currentDetailTools = [];
      this.render();
    },

    getMcpIcon(name, tier, size = 20) {
      const n = (name || '').toLowerCase();
      if (n.includes('supervisor') || n.includes('orchestrator')) {
        return getSvgIcon('cpu', { size, className: 'bx-mcp-icon-core' });
      }
      if (n.includes('docs') || n.includes('book')) {
        return getSvgIcon('book', { size, className: 'bx-mcp-icon-docs' });
      }
      if (n.includes('playwright') || n.includes('browser')) {
        return getSvgIcon('globe', { size, className: 'bx-mcp-icon-browser' });
      }
      if (n.includes('postmcp') || n.includes('api') || n.includes('http')) {
        return getSvgIcon('zap', { size, className: 'bx-mcp-icon-api' });
      }
      if (n.includes('dbhub') || n.includes('database') || n.includes('sql') || n.includes('postgres') || n.includes('sqlite')) {
        return getSvgIcon('database', { size, className: 'bx-mcp-icon-db' });
      }
      if (n.includes('redis') || n.includes('cache')) {
        return getSvgIcon('server', { size, className: 'bx-mcp-icon-redis' });
      }
      if (n.includes('semgrep') || n.includes('ast') || n.includes('lint')) {
        return getSvgIcon('shield', { size, className: 'bx-mcp-icon-security' });
      }
      if (n.includes('mobile') || n.includes('android') || n.includes('ios')) {
        return getSvgIcon('smartphone', { size, className: 'bx-mcp-icon-mobile' });
      }
      if (n.includes('desktop') || n.includes('window')) {
        return getSvgIcon('terminal', { size, className: 'bx-mcp-icon-desktop' });
      }
      if (n.includes('native') || n.includes('binary')) {
        return getSvgIcon('flask', { size, className: 'bx-mcp-icon-native' });
      }
      if (n.includes('github') || n.includes('gitlab')) {
        return getSvgIcon('gitPullRequest', { size, className: 'bx-mcp-icon-git' });
      }
      if (n.includes('docker') || n.includes('container')) {
        return getSvgIcon('package', { size, className: 'bx-mcp-icon-docker' });
      }
      if (n.includes('sentry')) {
        return getSvgIcon('alertTriangle', { size, className: 'bx-mcp-icon-sentry' });
      }
      if (n.includes('slack')) {
        return getSvgIcon('messageSquare', { size, className: 'bx-mcp-icon-slack' });
      }
      if (n.includes('search') || n.includes('brave')) {
        return getSvgIcon('search', { size, className: 'bx-mcp-icon-search' });
      }
      if (n.includes('fetch')) {
        return getSvgIcon('globe', { size, className: 'bx-mcp-icon-fetch' });
      }
      if (n.includes('filesystem')) {
        return getSvgIcon('folder', { size, className: 'bx-mcp-icon-fs' });
      }
      return getSvgIcon('plug', { size, className: 'bx-mcp-icon-default' });
    },

    getMcpAttributes(it) {
      const name = (it.name || it.id || '').toLowerCase();
      let domain = { label: '生态工具探针', icon: 'plug', badgeClass: 'bx-badge-gray' };
      if (name.includes('supervisor')) domain = { label: '核心调度控制面', icon: 'cpu', badgeClass: 'bx-badge-blue' };
      else if (name.includes('docs')) domain = { label: '工程文档与知识检索', icon: 'book', badgeClass: 'bx-badge-purple' };
      else if (name.includes('postmcp')) domain = { label: '接口审计与动态发包', icon: 'zap', badgeClass: 'bx-badge-cyan' };
      else if (name.includes('playwright')) domain = { label: '无头浏览器自动化', icon: 'globe', badgeClass: 'bx-badge-green' };
      else if (name.includes('dbhub')) domain = { label: '关系数据库 Schema', icon: 'database', badgeClass: 'bx-badge-orange' };
      else if (name.includes('redis')) domain = { label: '缓存与分布式状态', icon: 'server', badgeClass: 'bx-badge-red' };
      else if (name.includes('semgrep')) domain = { label: '静态代码污点扫描', icon: 'shield', badgeClass: 'bx-badge-purple' };
      else if (name.includes('mobile')) domain = { label: '移动端组件与 UI 探针', icon: 'smartphone', badgeClass: 'bx-badge-blue' };
      else if (it.category === 'devops' || name.includes('github') || name.includes('gitlab') || name.includes('docker')) {
        domain = { label: 'DevOps & 代码协同', icon: 'gitPullRequest', badgeClass: 'bx-badge-blue' };
      } else if (it.category === 'storage' || name.includes('postgres') || name.includes('sqlite')) {
        domain = { label: '关系型与嵌入存储', icon: 'database', badgeClass: 'bx-badge-orange' };
      } else if (it.category === 'tools' || name.includes('filesystem') || name.includes('slack') || name.includes('fetch')) {
        domain = { label: '沙箱与系统工具', icon: 'tool', badgeClass: 'bx-badge-cyan' };
      } else if (it.category === 'search' || name.includes('search') || name.includes('brave')) {
        domain = { label: '信息检索与情报', icon: 'search', badgeClass: 'bx-badge-purple' };
      } else if (it.category === 'security') {
        domain = { label: '安全防御与审计', icon: 'shield', badgeClass: 'bx-badge-red' };
      }

      const isBuiltin = it.tier === 'builtin' || it.source === 'builtin' || name.startsWith('bugx-');
      const isOnline = it.source === 'online' || it.tier === 'community' || Boolean(it.author);
      const tierObj = isBuiltin
        ? { label: '核心内建', icon: 'cpu', badgeClass: 'bx-badge-blue' }
        : (isOnline
          ? { label: '云端开源', icon: 'globe', badgeClass: 'bx-badge-purple' }
          : { label: '官方扩展', icon: 'package', badgeClass: 'bx-badge-gray' });

      const trustLabel = it.trust === 'builtin' ? '核心内建 (Builtin)' : `Trust ${it.trust || 'A'}`;

      const reqCreds = it.required_credentials || it.required_env || [];
      const credsReady = it.credentials_ready !== false;

      return {
        domain,
        tier: tierObj,
        trustLabel,
        reqCreds,
        credsReady,
        isBuiltin,
        isOnline
      };
    },

    renderSubFilters() {
      if (this.activeTab === 'local') {
        const filters = [
          { key: 'all', label: '全部已挂载', icon: 'server' },
          { key: 'active', label: '运行就绪', icon: 'checkCircle' },
          { key: 'disabled', label: '已停用', icon: 'slash' },
          { key: 'needs_creds', label: '需配置凭据', icon: 'key' },
          { key: 'builtin', label: '核心内建', icon: 'cpu' }
        ];
        return filters.map(f => `
          <button type="button" class="bx-subfilter-btn ${this.localFilter === f.key ? 'active' : ''}" data-filter="${f.key}">
            ${getSvgIcon(f.icon, { size: 12 })}
            <span>${escapeHtml(f.label)}</span>
          </button>
        `).join('');
      } else if (this.activeTab === 'market') {
        const filters = [
          { key: 'all', label: '全部探针', icon: 'store' },
          { key: 'core', label: '核心调度', icon: 'cpu' },
          { key: 'storage', label: '存储与队列', icon: 'database' },
          { key: 'testing', label: '自动化测试', icon: 'globe' },
          { key: 'security', label: '代码审计', icon: 'shield' },
          { key: 'mobile', label: '移动端侧', icon: 'smartphone' }
        ];
        return filters.map(f => `
          <button type="button" class="bx-subfilter-btn ${this.marketFilter === f.key ? 'active' : ''}" data-filter="${f.key}">
            ${getSvgIcon(f.icon, { size: 12 })}
            <span>${escapeHtml(f.label)}</span>
          </button>
        `).join('');
      } else {
        const filters = [
          { key: 'all', label: '全部在线', icon: 'globe' },
          { key: 'devops', label: 'DevOps/Git', icon: 'gitPullRequest' },
          { key: 'storage', label: '数据库/存储', icon: 'database' },
          { key: 'tools', label: '沙箱与系统', icon: 'tool' },
          { key: 'search', label: '检索与情报', icon: 'search' },
          { key: 'security', label: '安全审计', icon: 'shield' }
        ];
        return filters.map(f => `
          <button type="button" class="bx-subfilter-btn ${this.onlineFilter === f.key ? 'active' : ''}" data-filter="${f.key}">
            ${getSvgIcon(f.icon, { size: 12 })}
            <span>${escapeHtml(f.label)}</span>
          </button>
        `).join('');
      }
    },

    render() {
      const container = document.getElementById('bx-mcp-view-container');
      if (!container) return;

      if (this.viewMode === 'detail' && this.currentDetailMcp) {
        this.renderMcpDetail(container);
        return;
      }

      const q = (this.searchQuery || '').toLowerCase().trim();
      const mountedAll = this.mountedServers || [];
      const marketAll = this.marketplaceItems || [];
      const onlineAll = this.onlineItems || [];

      let filteredMounted = mountedAll.filter(s => {
        const isEnabled = s.enabled !== false && s.status !== 'disabled';
        const reqCreds = s.required_credentials || [];
        const isBuiltin = s.tier === 'builtin' || s.source === 'builtin' || (s.name || '').startsWith('bugx-');

        if (this.localFilter === 'active' && !isEnabled) return false;
        if (this.localFilter === 'disabled' && isEnabled) return false;
        if (this.localFilter === 'needs_creds' && (!reqCreds.length || s.credentials_ready)) return false;
        if (this.localFilter === 'builtin' && !isBuiltin) return false;

        if (q) {
          const text = (s.name + ' ' + (s.description || '') + ' ' + (s.command || '')).toLowerCase();
          return text.includes(q);
        }
        return true;
      });

      let filteredMarket = marketAll.filter(it => {
        const name = (it.name || it.id || '').toLowerCase();
        if (this.marketFilter === 'core' && !name.includes('supervisor') && !name.includes('docs')) return false;
        if (this.marketFilter === 'storage' && !name.includes('dbhub') && !name.includes('redis')) return false;
        if (this.marketFilter === 'testing' && !name.includes('playwright') && !name.includes('postmcp')) return false;
        if (this.marketFilter === 'security' && !name.includes('semgrep') && !name.includes('native')) return false;
        if (this.marketFilter === 'mobile' && !name.includes('mobile') && !name.includes('desktop')) return false;

        if (q) {
          const text = (it.name + ' ' + (it.description || '') + ' ' + (it.package || '') + ' ' + (it.command_preview || '')).toLowerCase();
          return text.includes(q);
        }
        return true;
      });

      let filteredOnline = onlineAll.filter(it => {
        const cat = (it.category || '').toLowerCase();
        if (this.onlineFilter === 'devops' && cat !== 'devops') return false;
        if (this.onlineFilter === 'storage' && cat !== 'storage') return false;
        if (this.onlineFilter === 'tools' && cat !== 'tools') return false;
        if (this.onlineFilter === 'search' && cat !== 'search') return false;
        if (this.onlineFilter === 'security' && cat !== 'security') return false;

        if (q) {
          const text = (it.name + ' ' + (it.title || '') + ' ' + (it.description || '') + ' ' + (it.author || '') + ' ' + (it.tags || []).join(' ')).toLowerCase();
          return text.includes(q);
        }
        return true;
      });

      container.innerHTML = `
        <div class="bx-skview-header">
          <div class="bx-skview-title-wrap">
            <div class="bx-skview-badge-pill">${getSvgIcon('plug', { size: 13 })} MODEL CONTEXT PROTOCOL</div>
            <h2 class="bx-skview-title">MCP 协议服务与生态市场</h2>
            <p class="bx-skview-desc">统一管理当前工程挂载的 MCP 服务器，发现并安装官方审计探针，支持受控沙箱凭据隔离与智能自动匹配。</p>
          </div>
          <div class="bx-skview-actions">
            <div class="bx-skview-search-box">
              <span class="bx-skview-search-icon">${getSvgIcon('search', { size: 14 })}</span>
              <input type="text" id="bx-mcpview-search" class="bx-skview-search-input" placeholder="搜索 MCP 服务名称、命令、工具或包名..." value="${escapeHtml(this.searchQuery)}" />
              ${this.searchQuery ? `<button type="button" id="bx-mcpview-clear-search" class="bx-skview-search-clear" title="清空搜索">${getSvgIcon('close', { size: 12 })}</button>` : ''}
            </div>
            <button type="button" id="bx-mcpview-add-custom-btn" class="bx-action-btn secondary" title="挂载自定义 command / stdio MCP 服务器">
              <span>${getSvgIcon('plus', { size: 14 })}</span><span>添加自定义 MCP</span>
            </button>
            <button type="button" id="bx-mcpview-automatch-btn" class="bx-action-btn primary" title="基于当前工程技术栈自动匹配适用的 MCP/Skill">
              <span>${getSvgIcon('sparkles', { size: 14 })}</span><span>AI 智能匹配</span>
            </button>
            <button type="button" id="bx-mcpview-back-chat-btn" class="bx-action-btn secondary" title="切回当前对话窗口">
              <span>${getSvgIcon('chat', { size: 14 })}</span><span>返回对话</span>
            </button>
          </div>
        </div>

        <div class="bx-skview-tabs-bar">
          <div class="bx-skview-tabs">
            <button type="button" class="bx-skview-tab ${this.activeTab === 'local' ? 'active' : ''}" data-tab="local">
              <span>${getSvgIcon('server', { size: 14 })}</span>
              <span>已挂载服务器</span>
              <span class="bx-skview-tab-count">${mountedAll.length}</span>
            </button>
            <button type="button" class="bx-skview-tab ${this.activeTab === 'market' ? 'active' : ''}" data-tab="market">
              <span>${getSvgIcon('store', { size: 14 })}</span>
              <span>官方推荐探针</span>
              <span class="bx-skview-tab-count">${marketAll.length}</span>
            </button>
            <button type="button" class="bx-skview-tab ${this.activeTab === 'online' ? 'active' : ''}" data-tab="online">
              <span>${getSvgIcon('globe', { size: 14 })}</span>
              <span>云端在线市场</span>
              <span class="bx-skview-tab-count">${onlineAll.length}</span>
            </button>
          </div>
          <div class="bx-skview-subfilters">
            ${this.renderSubFilters()}
          </div>
        </div>

        <div class="bx-skview-body">
          ${this.activeTab === 'local' ? this.renderMountedGrid(filteredMounted) : (this.activeTab === 'market' ? this.renderMarketGrid(filteredMarket) : this.renderOnlineGrid(filteredOnline))}
        </div>
      `;

      this.bindEvents(container);
    },

    renderMountedGrid(servers) {
      if (!servers || servers.length === 0) {
        return `
          <div class="bx-sk-empty-state">
            <div class="bx-sk-empty-icon">${getSvgIcon('plug', { size: 48 })}</div>
            <h3 class="bx-sk-empty-title">当前工程尚未挂载专属 MCP 服务器</h3>
            <p class="bx-sk-empty-desc">MCP (Model Context Protocol) 为智能体提供外部工具交互能力。您可以从 MCP 生态市场一键挂载官方审计组件，或使用 AI 智能匹配。</p>
            <div class="bx-sk-empty-actions">
              <button type="button" class="bx-action-btn primary" id="bx-mcp-empty-match-btn">
                ${getSvgIcon('sparkles', { size: 14 })}
                <span>AI 智能匹配适用 MCP</span>
              </button>
              <button type="button" class="bx-action-btn secondary" id="bx-mcp-empty-market-btn">
                ${getSvgIcon('store', { size: 14 })}
                <span>浏览 MCP 生态市场</span>
              </button>
            </div>
          </div>
        `;
      }

      const cardsHtml = servers.map(s => {
        const { domain, tier, trustLabel, reqCreds, credsReady, isBuiltin } = this.getMcpAttributes(s);
        const iconSvg = this.getMcpIcon(s.name, s.tier, 22);
        const isEnabled = s.enabled !== false && s.status !== 'disabled';
        const toolsCount = s.tool_count || s.tools || 0;
        const credStatusPill = !reqCreds.length
          ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 免额外凭据</span>`
          : (credsReady
            ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 凭据就绪</span>`
            : `<span class="bx-badge bx-badge-orange">${getSvgIcon('alertTriangle', { size: 11 })} 需配置凭据</span>`);

        return `
          <div class="bx-sk-card ${isEnabled ? '' : 'disabled'}" data-name="${escapeHtml(s.name)}">
            <div class="bx-sk-card-head" style="cursor:pointer;" data-name="${escapeHtml(s.name)}" title="点击查看 MCP 详细全景规范">
              <div class="bx-sk-avatar" title="${escapeHtml(s.name)}">${iconSvg}</div>
              <div class="bx-sk-title-col">
                <div class="bx-sk-title-row">
                  <span class="bx-sk-name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
                  <span class="bx-badge ${domain.badgeClass}">${getSvgIcon(domain.icon, { size: 11 })} ${domain.label}</span>
                </div>
              </div>
              <div class="bx-sk-toggle-wrap">
                <label class="bx-switch" title="${isEnabled ? '点击停用该 MCP' : '点击启用该 MCP'}">
                  <input type="checkbox" class="bx-mcp-toggle-cb" data-name="${escapeHtml(s.name)}" ${isEnabled ? 'checked' : ''} />
                  <span class="bx-slider"></span>
                </label>
              </div>
            </div>

            <div class="bx-sk-card-body bx-mcp-card-body-click" data-name="${escapeHtml(s.name)}" style="cursor:pointer;" title="点击查看 MCP 全景规范与工具手册">
              <p class="bx-sk-desc" title="${escapeHtml(s.description || MCP_DESCRIPTIONS[s.name] || '提供标准化的外部工具调用与受控通信通道。')}">
                ${escapeHtml(s.description || MCP_DESCRIPTIONS[s.name] || '提供标准化的外部工具调用与受控通信通道。')}
              </p>
              <div class="bx-sk-meta" title="${escapeHtml(s.command || 'stdio')} · ${toolsCount} 个原子工具">
                <span>${getSvgIcon('terminal', { size: 12 })}</span>
                <span>${escapeHtml(s.command || 'stdio')}</span>
                <span style="opacity: 0.35; margin: 0 4px;">•</span>
                <span>${getSvgIcon('cpu', { size: 12 })}</span>
                <span>${toolsCount} 个原子工具</span>
              </div>
            </div>

            <div class="bx-sk-card-footer">
              <div class="bx-sk-card-status">
                ${credStatusPill}
              </div>
              <div class="bx-sk-card-btns">
                <button type="button" class="bx-sk-action-btn secondary bx-mcp-detail-btn" data-name="${escapeHtml(s.name)}">
                  ${getSvgIcon('book', { size: 12 })}
                  <span>规范与工具</span>
                </button>
              </div>
            </div>
          </div>
        `;
      }).join('');

      let exploreCardHtml = '';
      if (servers.length > 0 && servers.length < 3) {
        exploreCardHtml = `
          <div class="bx-sk-card bx-mcp-explore-card" style="cursor:pointer;" title="前往生态市场浏览更多官方与开源 MCP 探针">
            <div class="bx-sk-card-head">
              <div class="bx-sk-avatar" style="background:rgba(2, 132, 199, 0.1); border-color:rgba(2, 132, 199, 0.25);">
                ${getSvgIcon('sparkles', { size: 20, className: 'bx-mcp-icon-core' })}
              </div>
              <div class="bx-sk-title-col">
                <div class="bx-sk-title-row">
                  <span class="bx-sk-name" style="font-family:inherit;">探索更多 MCP 探针</span>
                  <span class="bx-badge bx-badge-purple">${getSvgIcon('store', { size: 11 })} 生态市场</span>
                </div>
              </div>
            </div>
            <div class="bx-sk-card-body">
              <p class="bx-sk-desc">当前工程仅挂载了 ${servers.length} 个核心内置服务。前往生态市场可一键挂载 Playwright 自动化、Semgrep 静态代码审计、DBHub 数据库等官方探针与云端生态。</p>
              <div class="bx-sk-meta">
                <span>${getSvgIcon('globe', { size: 12 })}</span>
                <span>数十款开源与官方 MCP 服务即刻选用</span>
              </div>
            </div>
            <div class="bx-sk-card-footer">
              <div class="bx-sk-card-status">
                <span class="bx-badge bx-badge-blue">${getSvgIcon('plus', { size: 11 })} 即开即用</span>
              </div>
              <div class="bx-sk-card-btns">
                <button type="button" class="bx-sk-action-btn primary bx-mcp-explore-goto-btn">
                  ${getSvgIcon('store', { size: 12 })}
                  <span>浏览生态市场</span>
                </button>
              </div>
            </div>
          </div>
        `;
      }

      return `
        <div class="bx-sk-grid">
          ${cardsHtml}
          ${exploreCardHtml}
        </div>
      `;
    },

    renderMarketGrid(items) {
      if (!items || items.length === 0) {
        return `
          <div class="bx-sk-empty-state">
            <div class="bx-sk-empty-icon">${getSvgIcon('store', { size: 48 })}</div>
            <h3 class="bx-sk-empty-title">未找到匹配的 MCP 生态服务</h3>
            <p class="bx-sk-empty-desc">请尝试调整搜索关键词或选择其他分类标签。</p>
          </div>
        `;
      }

      return `
        <div class="bx-sk-grid">
          ${items.map(it => {
            const { domain, tier, trustLabel, reqCreds, credsReady } = this.getMcpAttributes(it);
            const iconSvg = this.getMcpIcon(it.name, it.tier, 22);
            const isMounted = it.mounted;
            const credStatusPill = !reqCreds.length
              ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 免额外凭据</span>`
              : (credsReady
                ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 凭据就绪</span>`
                : `<span class="bx-badge bx-badge-orange">${getSvgIcon('alertTriangle', { size: 11 })} 需配置凭据</span>`);

            return `
              <div class="bx-sk-card" data-name="${escapeHtml(it.name)}">
                <div class="bx-sk-card-head" style="cursor:pointer;" data-name="${escapeHtml(it.name)}" title="点击查看 MCP 详细全景规范">
                  <div class="bx-sk-avatar" title="${escapeHtml(it.name)}">${iconSvg}</div>
                  <div class="bx-sk-title-col">
                    <div class="bx-sk-title-row">
                      <span class="bx-sk-name" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
                      <span class="bx-badge ${domain.badgeClass}">${getSvgIcon(domain.icon, { size: 11 })} ${domain.label}</span>
                    </div>
                  </div>
                  <div class="bx-sk-toggle-wrap">
                    ${isMounted
                      ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 已挂载</span>`
                      : `<span class="bx-badge bx-badge-gray">${trustLabel}</span>`}
                  </div>
                </div>

                <div class="bx-sk-card-body bx-mcp-card-body-click" data-name="${escapeHtml(it.name)}" style="cursor:pointer;" title="点击查看 MCP 全景规范与工具手册">
                  <p class="bx-sk-desc" title="${escapeHtml(it.description || MCP_DESCRIPTIONS[it.name] || '提供标准化的外部工具调用能力。')}">
                    ${escapeHtml(it.description || MCP_DESCRIPTIONS[it.name] || '提供标准化的外部工具调用能力。')}
                  </p>
                  <div class="bx-sk-meta" title="${escapeHtml(it.package || it.installer || 'builtin')} · ${escapeHtml(it.command_preview || 'stdio')}">
                    <span>${getSvgIcon('package', { size: 12 })}</span>
                    <span>${escapeHtml(it.package || it.installer || 'builtin')}</span>
                    <span style="opacity: 0.35; margin: 0 4px;">•</span>
                    <span>${getSvgIcon('terminal', { size: 12 })}</span>
                    <span>${escapeHtml(it.command_preview || 'stdio')}</span>
                  </div>
                </div>

                <div class="bx-sk-card-footer">
                  <div class="bx-sk-card-status">
                    ${credStatusPill}
                  </div>
                  <div class="bx-sk-card-btns">
                    <button type="button" class="bx-sk-action-btn secondary bx-mcp-detail-btn" data-name="${escapeHtml(it.name)}">
                      ${getSvgIcon('book', { size: 12 })}
                      <span>查看规范</span>
                    </button>
                    ${isMounted ? `
                      <button type="button" class="bx-sk-action-btn danger bx-mcp-unmount-btn" data-name="${escapeHtml(it.name)}" title="从当前项目卸载该 MCP">
                        ${getSvgIcon('trash', { size: 12 })}
                        <span>卸载</span>
                      </button>
                    ` : `
                      <button type="button" class="bx-sk-action-btn primary bx-mcp-mount-btn" data-name="${escapeHtml(it.name)}" title="一键挂载到当前项目 .mcp.json">
                        ${getSvgIcon('plus', { size: 12 })}
                        <span>一键挂载</span>
                      </button>
                    `}
                  </div>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    },

    renderOnlineGrid(items) {
      if (!items || items.length === 0) {
        return `
          <div class="bx-sk-empty-state">
            <div class="bx-sk-empty-icon">${getSvgIcon('globe', { size: 48 })}</div>
            <h3 class="bx-sk-empty-title">未找到匹配的云端开源 MCP 服务</h3>
            <p class="bx-sk-empty-desc">请尝试调整搜索关键词或选择其他分类标签。</p>
          </div>
        `;
      }

      return `
        <div class="bx-sk-grid">
          ${items.map(it => {
            const { domain, tier, trustLabel, reqCreds, credsReady } = this.getMcpAttributes(it);
            const iconSvg = this.getMcpIcon(it.name, it.tier, 22);
            const isMounted = Boolean(it.mounted);
            const credStatusPill = !reqCreds.length
              ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 免额外凭据</span>`
              : (credsReady
                ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 凭据就绪</span>`
                : `<span class="bx-badge bx-badge-orange">${getSvgIcon('alertTriangle', { size: 11 })} 需配置凭据</span>`);

            return `
              <div class="bx-sk-card" data-name="${escapeHtml(it.name)}">
                <div class="bx-sk-card-head" style="cursor:pointer;" data-name="${escapeHtml(it.name)}" title="点击查看 MCP 详细全景规范">
                  <div class="bx-sk-avatar" title="${escapeHtml(it.name)}">${iconSvg}</div>
                  <div class="bx-sk-title-col">
                    <div class="bx-sk-title-row">
                      <span class="bx-sk-name" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
                      <span class="bx-badge ${domain.badgeClass}">${getSvgIcon(domain.icon, { size: 11 })} ${domain.label}</span>
                    </div>
                  </div>
                  <div class="bx-sk-toggle-wrap">
                    <span class="bx-sk-star-badge" title="开源星标关注度">${getSvgIcon('star', { size: 11 })} ${it.stars || 0}</span>
                    ${isMounted
                      ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 11 })} 已挂载</span>`
                      : `<span class="bx-badge bx-badge-purple" style="font-size:10px;">by ${escapeHtml(it.author || 'community')}</span>`}
                  </div>
                </div>

                <div class="bx-sk-card-body bx-mcp-card-body-click" data-name="${escapeHtml(it.name)}" style="cursor:pointer;" title="点击查看 MCP 全景规范与工具手册">
                  <p class="bx-sk-desc" title="${escapeHtml(it.description || '提供标准化的外部工具调用能力。')}">
                    ${escapeHtml(it.description || '提供标准化的外部工具调用能力。')}
                  </p>
                  <div class="bx-sk-meta" title="${escapeHtml(it.package || it.installer || 'npx')} · ${it.tools_count || (it.tools || []).length || 0} 个原子工具">
                    <span>${getSvgIcon('package', { size: 12 })}</span>
                    <span>${escapeHtml(it.package || it.installer || 'npx')}</span>
                    <span style="opacity: 0.35; margin: 0 4px;">•</span>
                    <span>${getSvgIcon('cpu', { size: 12 })}</span>
                    <span>${it.tools_count || (it.tools || []).length || 0} 个原子工具</span>
                  </div>
                </div>

                <div class="bx-sk-card-footer">
                  <div class="bx-sk-card-status">
                    ${credStatusPill}
                  </div>
                  <div class="bx-sk-card-btns">
                    <button type="button" class="bx-sk-action-btn secondary bx-mcp-detail-btn" data-name="${escapeHtml(it.name)}">
                      ${getSvgIcon('book', { size: 12 })}
                      <span>查看规范</span>
                    </button>
                    ${isMounted ? `
                      <button type="button" class="bx-sk-action-btn danger bx-mcp-unmount-btn" data-name="${escapeHtml(it.name)}" title="从当前项目卸载该 MCP">
                        ${getSvgIcon('trash', { size: 12 })}
                        <span>卸载</span>
                      </button>
                    ` : `
                      <button type="button" class="bx-sk-action-btn primary bx-mcp-mount-btn" data-name="${escapeHtml(it.name)}" title="一键挂载到当前项目 .mcp.json">
                        ${getSvgIcon('plus', { size: 12 })}
                        <span>一键挂载</span>
                      </button>
                    `}
                  </div>
                </div>
              </div>
            `;
          }).join('')}
        </div>
      `;
    },

    renderMcpDetail(container) {
      const it = this.currentDetailMcp;
      if (!it) {
        this.viewMode = 'list';
        this.render();
        return;
      }

      const { domain, tier, trustLabel, reqCreds, credsReady, isBuiltin } = this.getMcpAttributes(it);
      const iconSvg = this.getMcpIcon(it.name, it.tier, 28);
      const isMounted = it.mounted !== false && Boolean(it.mounted);
      const isEnabled = it.enabled !== false && it.status !== 'disabled';
      const rawContent = this.currentDetailContent || '';
      const renderedDoc = renderMarkdown(rawContent);

      const allTools = this.currentDetailTools || [];
      const q = (this.toolsFilterQuery || '').toLowerCase().trim();
      const filteredTools = q
        ? allTools.filter(t => (t.name + ' ' + (t.description || '') + ' ' + (t.parameters || '')).toLowerCase().includes(q))
        : allTools;

      const displayCommand = it.command_preview || it.command || `bugx mcp-runtime run ${it.name}`;
      const sandboxPath = `~/.bugx/sandboxes/${escapeHtml(currentHash || 'project')}/mcp_env.json`;

      container.innerHTML = `
        <div class="bx-sk-detail-view">
          <!-- Top Navigation & Breadcrumbs -->
          <div class="bx-sk-detail-top-bar">
            <div class="bx-sk-detail-nav-left">
              <button type="button" class="bx-sk-detail-back-btn" id="bx-mcp-detail-back-btn" title="返回 MCP 列表">
                ${getSvgIcon('arrowLeft', { size: 14 })}
                <span>返回 MCP 列表</span>
              </button>
              <div class="bx-sk-detail-breadcrumbs">
                <span>MCP 协议中心</span>
                <span>/</span>
                <span>${this.activeTab === 'local' ? '已挂载服务器' : (this.activeTab === 'market' ? '官方推荐探针' : '云端在线市场')}</span>
                <span>/</span>
                <span class="active">${escapeHtml(it.name)}</span>
              </div>
            </div>
            <div class="bx-sk-detail-nav-actions">
              <button type="button" class="bx-action-btn secondary btn-sm" id="bx-mcp-detail-copy-cmd" title="复制启动命令行">
                ${getSvgIcon('terminal', { size: 13 })}
                <span>复制启动命令</span>
              </button>
              <button type="button" class="bx-action-btn secondary btn-sm" id="bx-mcp-detail-copy-cfg" title="复制 .mcp.json 配置片段">
                ${getSvgIcon('copy', { size: 13 })}
                <span>复制配置 JSON</span>
              </button>
              <button type="button" class="bx-action-btn primary btn-sm" id="bx-mcp-detail-use-chat" title="返回并填入当前对话框">
                ${getSvgIcon('chat', { size: 13 })}
                <span>填入当前对话</span>
              </button>
            </div>
          </div>

          <!-- Hero Header -->
          <div class="bx-sk-detail-hero">
            <div class="bx-sk-detail-avatar">
              ${iconSvg}
            </div>
            <div class="bx-sk-detail-title-col">
              <div class="bx-sk-detail-name-row">
                <h1 class="bx-sk-detail-name">${escapeHtml(it.name)}</h1>
                <span class="bx-badge ${domain.badgeClass}">${getSvgIcon(domain.icon, { size: 12 })} ${domain.label}</span>
                <span class="bx-badge ${tier.badgeClass}">${getSvgIcon(tier.icon, { size: 12 })} ${tier.label}</span>
                <span class="bx-badge bx-badge-gray">${trustLabel}</span>
                ${isEnabled ? `<span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 12 })} 运行就绪</span>` : `<span class="bx-badge bx-badge-gray">已停用</span>`}
                ${allTools.length ? `<span class="bx-badge bx-badge-blue">${getSvgIcon('cpu', { size: 12 })} ${allTools.length} 个原子工具</span>` : ''}
              </div>
              <p class="bx-sk-detail-desc">${escapeHtml(it.description || MCP_DESCRIPTIONS[it.name] || '提供标准化的外部工具调用能力与沙箱隔离通信。')}</p>
              <div class="bx-sk-detail-meta-row">
                <span class="bx-sk-meta-item" title="${escapeHtml(displayCommand)}">
                  ${getSvgIcon('terminal', { size: 13 })}
                  <span>${escapeHtml(displayCommand)}</span>
                </span>
                <span class="bx-sk-meta-item">
                  ${getSvgIcon('package', { size: 13 })}
                  <span>载荷: ${escapeHtml(it.package || it.installer || 'builtin')}</span>
                </span>
                <span class="bx-sk-meta-item">
                  ${getSvgIcon('shield', { size: 13 })}
                  <span>传输: stdio (JSON-RPC 2.0)</span>
                </span>
              </div>
            </div>
          </div>

          <!-- Main Content Grid -->
          <div class="bx-sk-detail-content-grid">
            <!-- Left: Documentation & Tools Matrix -->
            <div class="bx-sk-detail-main">
              <!-- Card: MCP Documentation -->
              <div class="bx-sk-doc-card">
                <div class="bx-sk-doc-card-header">
                  <div class="bx-sk-doc-header-title">
                    ${getSvgIcon('book', { size: 16 })}
                    <span>MCP 服务规范与接入指南</span>
                  </div>
                  <div class="bx-sk-doc-header-actions">
                    <button type="button" class="bx-action-btn secondary btn-sm" id="bx-mcp-doc-copy-raw">
                      ${getSvgIcon('copy', { size: 12 })}
                      <span>复制规范 Markdown</span>
                    </button>
                  </div>
                </div>
                <div class="bx-sk-markdown-body" id="bx-mcp-markdown-body">
                  ${this.currentDetailLoading ? `
                    <div style="text-align:center; padding: 60px 20px; color: var(--app-secondary-foreground, #64748b);">
                      <div style="font-size: 1.8rem; margin-bottom: 12px; display: inline-block;">
                        ${getSvgIcon('sparkles', { size: 32 })}
                      </div>
                      <div>正在加载 MCP 协议规范与工具矩阵...</div>
                    </div>
                  ` : renderedDoc}
                </div>
              </div>

              <!-- Card: Registered Tools Matrix -->
              <div class="bx-sk-doc-card" style="margin-top:20px;">
                <div class="bx-sk-doc-card-header">
                  <div class="bx-sk-doc-header-title">
                    ${getSvgIcon('cpu', { size: 16 })}
                    <span>注册原子工具矩阵 (Registered Tools · ${allTools.length})</span>
                  </div>
                  <div class="bx-sk-doc-header-actions">
                    <input type="text" id="bx-mcp-tools-search" class="bx-skview-search-input" style="width:180px; padding:4px 10px; font-size:0.75rem;" placeholder="过滤工具名称..." value="${escapeHtml(this.toolsFilterQuery)}" />
                  </div>
                </div>
                <div style="padding:16px 20px;">
                  ${filteredTools.length ? `
                    <div style="display:flex; flex-direction:column; gap:10px;">
                      ${filteredTools.map(t => `
                        <div class="bx-mcp-tool-row">
                          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                            <code class="bx-sk-cmd-pill bx-mcp-tool-copy" data-name="${escapeHtml(t.name)}" title="点击复制工具调用名">${escapeHtml(t.name)}</code>
                            <span class="bx-badge bx-badge-gray" style="font-size:0.7rem;">params: ${escapeHtml(t.parameters || 'void')}</span>
                          </div>
                          <div style="font-size:0.8rem; color:var(--app-secondary-foreground, #64748b); line-height:1.4;">${escapeHtml(t.description || '无详细描述')}</div>
                        </div>
                      `).join('')}
                    </div>
                  ` : `
                    <div style="color:var(--app-secondary-foreground, #64748b); font-size:0.85rem; padding:12px 0;">未找到匹配的工具定义。</div>
                  `}
                </div>
              </div>
            </div>

            <!-- Right: Attribute Classification & Settings -->
            <div class="bx-sk-detail-sidebar">
              <!-- Card 1: 属性归类与特征档案 -->
              <div class="bx-sk-sidebar-card bx-sk-prop-card">
                <div class="bx-sk-sidebar-card-title">
                  ${getSvgIcon('tag', { size: 16 })}
                  <span>属性归类与特征档案</span>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">业务领域 (Domain)</div>
                  <div class="bx-sk-prop-value" style="display:flex; align-items:center; gap:6px;">
                    ${getSvgIcon(domain.icon, { size: 14 })}
                    <span style="font-weight:600;">${domain.label}</span>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">传输协议 (Transport)</div>
                  <div class="bx-sk-prop-value" style="display:flex; align-items:center; gap:6px;">
                    ${getSvgIcon('terminal', { size: 14 })}
                    <span>stdio (Standard Input/Output)</span>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">信任等级 (Trust Baseline)</div>
                  <div class="bx-sk-prop-value" style="display:flex; align-items:center; gap:6px;">
                    ${getSvgIcon('shield', { size: 14 })}
                    <span>${trustLabel}</span>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">软件包与载荷 (Package)</div>
                  <div class="bx-sk-prop-value">
                    <code class="bx-sk-cmd-pill">${escapeHtml(it.package || it.installer || 'builtin')}</code>
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">受控沙箱隔离路径 (0600)</div>
                  <div class="bx-sk-path-box" id="bx-mcp-sandbox-copy" title="点击复制沙箱路径">
                    <span>${escapeHtml(sandboxPath)}</span>
                    ${getSvgIcon('copy', { size: 12 })}
                  </div>
                </div>
                <div class="bx-sk-prop-item">
                  <div class="bx-sk-prop-label">凭据需求与状态</div>
                  <div style="margin-top:6px;">
                    ${!reqCreds.length ? `
                      <span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 12 })} 免额外环境变量凭据</span>
                    ` : (credsReady ? `
                      <span class="bx-badge bx-badge-green">${getSvgIcon('checkCircle', { size: 12 })} 凭据已在沙箱就绪 (${reqCreds.join(', ')})</span>
                    ` : `
                      <span class="bx-badge bx-badge-orange">${getSvgIcon('alertTriangle', { size: 12 })} 缺少凭据 (${reqCreds.join(', ')})</span>
                    `)}
                  </div>
                </div>
              </div>

              <!-- Card 2: MCP 控制与流转设置 -->
              <div class="bx-sk-sidebar-card bx-sk-settings-card">
                <div class="bx-sk-sidebar-card-title">
                  ${getSvgIcon('settings', { size: 16 })}
                  <span>MCP 控制与流转设置</span>
                </div>

                <!-- Setting: Enable / Disable -->
                <div class="bx-sk-prop-item">
                  <div style="display:flex; justify-content:space-between; align-items:center;">
                    <div>
                      <div style="font-size:0.82rem; font-weight:700;">服务激活状态</div>
                      <div style="font-size:0.72rem; color:var(--app-secondary-foreground, #64748b); margin-top:2px;">
                        ${isBuiltin ? '核心内建调度通道' : (isEnabled ? '当前已挂载并在会话中响应' : '已停用，不向宿主提供工具')}
                      </div>
                    </div>
                    <div>
                      <label class="bx-switch" title="切换 MCP 启用状态">
                        <input type="checkbox" id="bx-mcp-detail-toggle-cb" ${isEnabled ? 'checked' : ''} />
                        <span class="bx-slider"></span>
                      </label>
                    </div>
                  </div>
                </div>

                <!-- Setting: Mount / Unmount -->
                <div class="bx-sk-prop-item" style="margin-top:16px; padding-top:16px; border-top:1px solid var(--border, rgba(148,163,184,0.12));">
                  <div style="font-size:0.82rem; font-weight:700; margin-bottom:8px;">工程挂载管理 (.mcp.json)</div>
                  <div style="display:flex; flex-direction:column; gap:8px;">
                    ${isMounted ? `
                      <button type="button" class="bx-action-btn danger w-full" id="bx-mcp-detail-unmount-btn">
                        ${getSvgIcon('trash', { size: 14 })}
                        <span>从当前工程卸载该 MCP</span>
                      </button>
                    ` : `
                      <button type="button" class="bx-action-btn primary w-full" id="bx-mcp-detail-mount-btn">
                        ${getSvgIcon('rocket', { size: 14 })}
                        <span>挂载到当前工程 (.mcp.json)</span>
                      </button>
                    `}
                    <button type="button" class="bx-action-btn secondary w-full" id="bx-mcp-detail-open-chat-btn">
                      ${getSvgIcon('chat', { size: 14 })}
                      <span>返回对话控制台</span>
                    </button>
                  </div>
                </div>

                <!-- Setting: Sandboxed Credentials Form -->
                ${reqCreds.length ? `
                  <div class="bx-sk-prop-item" style="margin-top:16px; padding-top:16px; border-top:1px solid var(--border, rgba(148,163,184,0.12));">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
                      <div style="font-size:0.82rem; font-weight:700;">沙箱凭据安全配置</div>
                      <span class="bx-badge bx-badge-blue" style="font-size:0.65rem;">0600 加密隔离</span>
                    </div>
                    <p style="font-size:0.72rem; color:var(--app-secondary-foreground, #64748b); margin-bottom:12px; line-height:1.4;">
                      密钥仅加密保存在 <code>~/.bugx/sandboxes/</code>，严禁明文落盘于项目 <code>.mcp.json</code>。
                    </p>
                    <div style="display:flex; flex-direction:column; gap:10px;">
                      ${reqCreds.map(c => `
                        <div>
                          <label style="display:block; font-size:0.72rem; font-weight:700; margin-bottom:3px; color:var(--app-primary-foreground, #0f172a);">${escapeHtml(c)}</label>
                          <div style="display:flex; align-items:center; gap:6px;">
                            <input type="password" class="bx-mcp-cred-input" data-key="${escapeHtml(c)}" placeholder="${it.configured_credentials && it.configured_credentials.includes(c) ? '●●●●●●●● (已配置，留空保持不变)' : '请输入密钥或连接串...'}" style="flex:1; padding:6px 10px; font-size:0.75rem; border:1px solid var(--border, #cbd5e1); border-radius:6px; background:var(--app-card-background, #fff); color:inherit;" />
                            <button type="button" class="bx-action-btn secondary btn-sm bx-mcp-toggle-pwd" title="查看/隐藏">
                              ${getSvgIcon('eye', { size: 12 })}
                            </button>
                          </div>
                        </div>
                      `).join('')}
                      <button type="button" class="bx-action-btn primary w-full" id="bx-mcp-save-creds-btn" style="margin-top:4px;">
                        ${getSvgIcon('lock', { size: 13 })}
                        <span>保存凭据到受控沙箱</span>
                      </button>
                    </div>
                  </div>
                ` : ''}
              </div>
            </div>
          </div>
        </div>
      `;

      this.bindDetailEvents(container, it);
    },

    bindEvents(container) {
      const searchInp = container.querySelector('#bx-mcpview-search');
      if (searchInp) {
        searchInp.oninput = (e) => {
          this.searchQuery = e.target.value;
          this.render();
          const newInp = container.querySelector('#bx-mcpview-search');
          if (newInp) {
            newInp.focus();
            newInp.setSelectionRange(newInp.value.length, newInp.value.length);
          }
        };
      }

      const clearSearch = container.querySelector('#bx-mcpview-clear-search');
      if (clearSearch) {
        clearSearch.onclick = () => {
          this.searchQuery = '';
          this.render();
        };
      }

      const tabBtns = container.querySelectorAll('.bx-skview-tab');
      tabBtns.forEach(btn => {
        btn.onclick = () => {
          this.activeTab = btn.dataset.tab;
          this.render();
        };
      });

      const subfilters = container.querySelectorAll('.bx-subfilter-btn');
      subfilters.forEach(btn => {
        btn.onclick = () => {
          if (this.activeTab === 'local') this.localFilter = btn.dataset.filter;
          else if (this.activeTab === 'market') this.marketFilter = btn.dataset.filter;
          else this.onlineFilter = btn.dataset.filter;
          this.render();
        };
      });

      // Card head clicks -> open detail
      const headClicks = container.querySelectorAll('.bx-sk-card-head');
      headClicks.forEach(head => {
        head.onclick = (e) => {
          if (e.target.closest('.bx-sk-toggle-wrap') || e.target.closest('.bx-switch') || e.target.closest('input')) return;
          if (head.dataset.name) this.openMcpDetail(head.dataset.name);
        };
      });

      // Explore card clicks
      const exploreCard = container.querySelector('.bx-mcp-explore-card');
      if (exploreCard) {
        exploreCard.onclick = () => {
          this.activeTab = 'market';
          this.render();
        };
      }
      const exploreGoto = container.querySelector('.bx-mcp-explore-goto-btn');
      if (exploreGoto) {
        exploreGoto.onclick = (e) => {
          e.stopPropagation();
          this.activeTab = 'market';
          this.render();
        };
      }

      const matchBtn = container.querySelector('#bx-mcpview-automatch-btn');
      if (matchBtn) matchBtn.onclick = () => runCapabilityAutoMatch(true);

      const addCustomBtn = container.querySelector('#bx-mcpview-add-custom-btn');
      if (addCustomBtn) addCustomBtn.onclick = () => this.promptAddCustomMcp();

      const backChatBtn = container.querySelector('#bx-mcpview-back-chat-btn');
      if (backChatBtn) backChatBtn.onclick = () => this.close();

      const emptyMatch = container.querySelector('#bx-mcp-empty-match-btn');
      if (emptyMatch) emptyMatch.onclick = () => runCapabilityAutoMatch(true);

      const emptyMarket = container.querySelector('#bx-mcp-empty-market-btn');
      if (emptyMarket) emptyMarket.onclick = () => {
        this.activeTab = 'market';
        this.render();
      };

      // Card body clicks -> open detail
      const bodyClicks = container.querySelectorAll('.bx-mcp-card-body-click');
      bodyClicks.forEach(body => {
        body.onclick = () => this.openMcpDetail(body.dataset.name);
      });

      const detailBtns = container.querySelectorAll('.bx-mcp-detail-btn');
      detailBtns.forEach(btn => {
        btn.onclick = (e) => {
          e.stopPropagation();
          this.openMcpDetail(btn.dataset.name);
        };
      });

      // Toggle checkbox on card
      const toggleCbs = container.querySelectorAll('.bx-mcp-toggle-cb');
      toggleCbs.forEach(cb => {
        cb.onchange = async (e) => {
          e.stopPropagation();
          const srvName = cb.dataset.name;
          const checked = cb.checked;
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, server: srvName, enabled: checked, mount: true })
            });
            if (res.ok) {
              showToast(`已${checked ? '启用' : '停用'} MCP: ${srvName}`);
              await this.loadData();
              this.render();
            } else {
              showToast('操作失败');
              cb.checked = !checked;
            }
          } catch (err) {
            showToast('请求异常: ' + err.message);
            cb.checked = !checked;
          }
        };
      });

      // Mount button on card
      const mountBtns = container.querySelectorAll('.bx-mcp-mount-btn');
      mountBtns.forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const srvName = btn.dataset.name;
          const onItem = (this.onlineItems || []).find(o => o.name === srvName || o.id === srvName);
          const payload = {
            project_hash: currentHash,
            server: srvName,
            mount: true,
            enabled: true
          };
          if (onItem) {
            payload.command = onItem.command;
            payload.args = onItem.args;
          }
          btn.disabled = true;
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload)
            });
            if (res.ok) {
              showToast(`已成功挂载 MCP: ${srvName}`);
              await this.loadData();
              this.render();
            } else {
              showToast('挂载失败');
            }
          } catch (err) {
            showToast('挂载异常: ' + err.message);
          } finally {
            btn.disabled = false;
          }
        };
      });

      // Unmount button on card
      const unmountBtns = container.querySelectorAll('.bx-mcp-unmount-btn');
      unmountBtns.forEach(btn => {
        btn.onclick = async (e) => {
          e.stopPropagation();
          const srvName = btn.dataset.name;
          if (!confirm(`确定要从当前工程卸载 MCP "${srvName}" 吗？`)) return;
          btn.disabled = true;
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, server: srvName, mount: false })
            });
            if (res.ok) {
              showToast(`已从当前工程卸载 MCP: ${srvName}`);
              await this.loadData();
              this.render();
            } else {
              showToast('卸载失败');
            }
          } catch (err) {
            showToast('卸载异常: ' + err.message);
          } finally {
            btn.disabled = false;
          }
        };
      });
    },

    bindDetailEvents(container, it) {
      const backBtn = container.querySelector('#bx-mcp-detail-back-btn');
      if (backBtn) backBtn.onclick = () => this.closeMcpDetail();

      const copyCmd = container.querySelector('#bx-mcp-detail-copy-cmd');
      if (copyCmd) {
        copyCmd.onclick = () => {
          const cmd = it.command_preview || it.command || `bugx mcp-runtime run ${it.name}`;
          navigator.clipboard.writeText(cmd);
          showToast(`已复制命令: ${cmd}`);
        };
      }

      const copyCfg = container.querySelector('#bx-mcp-detail-copy-cfg');
      if (copyCfg) {
        copyCfg.onclick = () => {
          const cfg = JSON.stringify({
            mcpServers: {
              [it.name]: {
                command: it.command || 'stdio',
                args: []
              }
            }
          }, null, 2);
          navigator.clipboard.writeText(cfg);
          showToast('已复制 .mcp.json 配置片段');
        };
      }

      const useChat = container.querySelector('#bx-mcp-detail-use-chat');
      if (useChat) {
        useChat.onclick = () => {
          this.close();
          const composerInput = document.querySelector('.composer-textarea, textarea.input, #composer-input');
          if (composerInput) {
            composerInput.value = (composerInput.value ? composerInput.value + ' ' : '') + `@mcp:${it.name} `;
            composerInput.focus();
          }
        };
      }

      const docCopyRaw = container.querySelector('#bx-mcp-doc-copy-raw');
      if (docCopyRaw) {
        docCopyRaw.onclick = () => {
          navigator.clipboard.writeText(this.currentDetailContent || '');
          showToast('已复制规范 Markdown 源码');
        };
      }

      const toolsSearch = container.querySelector('#bx-mcp-tools-search');
      if (toolsSearch) {
        toolsSearch.oninput = (e) => {
          this.toolsFilterQuery = e.target.value;
          this.render();
          const newInp = container.querySelector('#bx-mcp-tools-search');
          if (newInp) {
            newInp.focus();
            newInp.setSelectionRange(newInp.value.length, newInp.value.length);
          }
        };
      }

      // Copy tool name
      const toolCopies = container.querySelectorAll('.bx-mcp-tool-copy');
      toolCopies.forEach(el => {
        el.onclick = () => {
          const tName = el.dataset.name;
          navigator.clipboard.writeText(tName);
          showToast(`已复制工具名称: ${tName}`);
        };
      });

      // Sandbox path copy
      const sandboxCopy = container.querySelector('#bx-mcp-sandbox-copy');
      if (sandboxCopy) {
        sandboxCopy.onclick = () => {
          const path = sandboxCopy.querySelector('span')?.textContent || '';
          navigator.clipboard.writeText(path);
          showToast(`已复制沙箱路径: ${path}`);
        };
      }

      // Toggle enabled switch in detail
      const toggleCb = container.querySelector('#bx-mcp-detail-toggle-cb');
      if (toggleCb) {
        toggleCb.onchange = async () => {
          const checked = toggleCb.checked;
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, server: it.name, enabled: checked, mount: true })
            });
            if (res.ok) {
              it.enabled = checked;
              showToast(`已${checked ? '启用' : '停用'} MCP: ${it.name}`);
              await this.loadData();
              this.render();
            } else {
              showToast('操作失败');
              toggleCb.checked = !checked;
            }
          } catch (err) {
            showToast('请求异常: ' + err.message);
            toggleCb.checked = !checked;
          }
        };
      }

      // Mount button in detail
      const mountBtn = container.querySelector('#bx-mcp-detail-mount-btn');
      if (mountBtn) {
        mountBtn.onclick = async () => {
          mountBtn.disabled = true;
          const onItem = (this.onlineItems || []).find(o => o.name === it.name || o.id === it.name);
          const payload = {
            project_hash: currentHash,
            server: it.name,
            mount: true,
            enabled: true
          };
          if (onItem) {
            payload.command = onItem.command;
            payload.args = onItem.args;
          }
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload)
            });
            if (res.ok) {
              it.mounted = true;
              it.enabled = true;
              showToast(`已成功挂载 MCP: ${it.name}`);
              await this.loadData();
              this.render();
            } else {
              showToast('挂载失败');
            }
          } catch (err) {
            showToast('挂载异常: ' + err.message);
          } finally {
            mountBtn.disabled = false;
          }
        };
      }

      // Unmount button in detail
      const unmountBtn = container.querySelector('#bx-mcp-detail-unmount-btn');
      if (unmountBtn) {
        unmountBtn.onclick = async () => {
          if (!confirm(`确定要从当前工程卸载 MCP "${it.name}" 吗？`)) return;
          unmountBtn.disabled = true;
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, server: it.name, mount: false })
            });
            if (res.ok) {
              it.mounted = false;
              it.enabled = false;
              showToast(`已卸载 MCP: ${it.name}`);
              await this.loadData();
              this.render();
            } else {
              showToast('卸载失败');
            }
          } catch (err) {
            showToast('卸载异常: ' + err.message);
          } finally {
            unmountBtn.disabled = false;
          }
        };
      }

      const openChatBtn = container.querySelector('#bx-mcp-detail-open-chat-btn');
      if (openChatBtn) openChatBtn.onclick = () => this.close();

      // Toggle password eye
      const eyeBtns = container.querySelectorAll('.bx-mcp-toggle-pwd');
      eyeBtns.forEach(btn => {
        btn.onclick = () => {
          const inp = btn.previousElementSibling;
          if (inp && inp.type === 'password') {
            inp.type = 'text';
            btn.innerHTML = getSvgIcon('eyeOff', { size: 12 });
          } else if (inp) {
            inp.type = 'password';
            btn.innerHTML = getSvgIcon('eye', { size: 12 });
          }
        };
      });

      // Save credentials button
      const saveCredsBtn = container.querySelector('#bx-mcp-save-creds-btn');
      if (saveCredsBtn) {
        saveCredsBtn.onclick = async () => {
          const inputs = container.querySelectorAll('.bx-mcp-cred-input');
          const envMap = {};
          let filledCount = 0;
          inputs.forEach(inp => {
            const k = inp.dataset.key;
            const v = inp.value.trim();
            if (v) {
              envMap[k] = v;
              filledCount++;
            }
          });
          if (filledCount === 0) {
            showToast('请输入要更新的环境变量密钥');
            return;
          }
          saveCredsBtn.disabled = true;
          saveCredsBtn.innerHTML = `${getSvgIcon('sparkles', { size: 13 })} 正在隔离存储...`;
          try {
            const res = await fetch('/api/mcp/configure', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ project_hash: currentHash, server: it.name, env: envMap })
            });
            if (res.ok) {
              showToast(`凭据已加密保存至 ~/.bugx/sandboxes/，绝不明文写入项目 .mcp.json`);
              await this.openMcpDetail(it.name, it);
            } else {
              showToast('保存凭据失败');
            }
          } catch (err) {
            showToast('保存异常: ' + err.message);
          } finally {
            saveCredsBtn.disabled = false;
            saveCredsBtn.innerHTML = `${getSvgIcon('lock', { size: 13 })} <span>保存凭据到受控沙箱</span>`;
          }
        };
      }
    },

    promptAddCustomMcp() {
      let modal = document.getElementById('bx-mcp-custom-modal');
      if (modal) modal.remove();

      modal = document.createElement('div');
      modal.id = 'bx-mcp-custom-modal';
      modal.className = 'modal-overlay';
      modal.innerHTML = `
        <div class="modal-card" style="max-width:540px;">
          <div class="modal-header">
            <span class="bx-sk-svg">${getSvgIcon('plug', { size: 18 })}</span>
            <h3>挂载自定义 MCP 服务器 (.mcp.json)</h3>
            <button type="button" class="ghost-btn modal-close" id="bx-mcp-custom-close">×</button>
          </div>
          <div class="modal-body" style="display:flex; flex-direction:column; gap:14px; padding:20px;">
            <p style="font-size:0.8rem; color:var(--app-secondary-foreground, #64748b); margin:0;">
              向当前工程根目录的 <code>.mcp.json</code> 添加自定义 MCP 协议通道，遵循 Model Context Protocol 标准。
            </p>
            <div>
              <label style="display:block; font-size:0.75rem; font-weight:700; margin-bottom:4px;">服务标识名 (Server Name) *</label>
              <input type="text" id="bx-custom-mcp-name" class="bx-skview-search-input" style="width:100%; border:1px solid var(--border, #cbd5e1); border-radius:6px; padding:6px 10px;" placeholder="例如: custom-analyzer, github-tools" />
            </div>
            <div>
              <label style="display:block; font-size:0.75rem; font-weight:700; margin-bottom:4px;">执行命令 (Command) *</label>
              <input type="text" id="bx-custom-mcp-cmd" class="bx-skview-search-input" style="width:100%; border:1px solid var(--border, #cbd5e1); border-radius:6px; padding:6px 10px;" placeholder="例如: npx, python3, uvx, node" value="npx" />
            </div>
            <div>
              <label style="display:block; font-size:0.75rem; font-weight:700; margin-bottom:4px;">执行参数 (Args, 空格分隔)</label>
              <input type="text" id="bx-custom-mcp-args" class="bx-skview-search-input" style="width:100%; border:1px solid var(--border, #cbd5e1); border-radius:6px; padding:6px 10px;" placeholder="例如: -y @modelcontextprotocol/server-memory" />
            </div>
          </div>
          <div class="modal-footer" style="display:flex; justify-content:flex-end; gap:8px;">
            <button type="button" class="bx-action-btn secondary" id="bx-mcp-custom-cancel">取消</button>
            <button type="button" class="bx-action-btn primary" id="bx-mcp-custom-save">
              ${getSvgIcon('plus', { size: 14 })}
              <span>保存并挂载</span>
            </button>
          </div>
        </div>
      `;
      document.body.appendChild(modal);

      const closeBtn = modal.querySelector('#bx-mcp-custom-close');
      const cancelBtn = modal.querySelector('#bx-mcp-custom-cancel');
      const saveBtn = modal.querySelector('#bx-mcp-custom-save');
      const closeModal = () => modal.remove();

      if (closeBtn) closeBtn.onclick = closeModal;
      if (cancelBtn) cancelBtn.onclick = closeModal;

      if (saveBtn) {
        saveBtn.onclick = async () => {
          const name = modal.querySelector('#bx-custom-mcp-name')?.value.trim();
          const cmd = modal.querySelector('#bx-custom-mcp-cmd')?.value.trim();
          const argsStr = modal.querySelector('#bx-custom-mcp-args')?.value.trim();
          if (!name || !cmd) {
            showToast('请填写服务标识名和启动命令');
            return;
          }
          const args = argsStr ? argsStr.split(/\s+/) : [];
          saveBtn.disabled = true;
          try {
            const res = await fetch('/api/mcp/toggle', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                project_hash: currentHash,
                server: name,
                command: cmd,
                args: args,
                mount: true,
                enabled: true
              })
            });
            if (res.ok) {
              showToast(`成功挂载自定义 MCP: ${name}`);
              closeModal();
              await this.loadData();
              this.render();
            } else {
              showToast('添加自定义 MCP 失败');
            }
          } catch (err) {
            showToast('添加异常: ' + err.message);
          } finally {
            saveBtn.disabled = false;
          }
        };
      }
    }
  }

  // =========================================================================
  // Files Center View (Primary Workspace File Governance & Deletion)
  // =========================================================================
  const FilesCenter = {
    isOpen: false,
    currentPath: '',
    currentWorkspace: '',
    currentProjectHash: '',
    entries: [],
    searchQuery: '',
    projects: [],
    isLoading: false,

    init() {
      const mainCol = document.querySelector('.main-column');
      if (!mainCol) return;
      let container = document.getElementById('bx-files-view-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'bx-files-view-container';
        container.className = 'bx-files-view-container';
        mainCol.appendChild(container);
      }
    },

    async open(targetPath = '') {
      if (typeof SkillsCenter !== 'undefined' && SkillsCenter.isOpen) SkillsCenter.close();
      if (typeof McpCenter !== 'undefined' && McpCenter.isOpen) McpCenter.close();
      this.init();
      this.isOpen = true;
      const mainCol = document.querySelector('.main-column');
      if (mainCol) mainCol.classList.add('bx-files-view-active');
      this.updateSidebarButtonState(true);
      await this.loadProjects();
      if (targetPath) {
        this.currentPath = targetPath;
      } else if (!this.currentPath && this.currentWorkspace) {
        this.currentPath = this.currentWorkspace;
      }
      await this.loadData(this.currentPath);
      this.render();
    },

    close() {
      this.isOpen = false;
      const mainCol = document.querySelector('.main-column');
      if (mainCol) mainCol.classList.remove('bx-files-view-active');
      this.updateSidebarButtonState(false);
    },

    toggle(targetPath = '') {
      if (this.isOpen) this.close();
      else this.open(targetPath);
    },

    updateSidebarButtonState(active) {
      const actions = document.querySelectorAll('.bx-files-sidebar-action, .bx-group-files-btn');
      actions.forEach(btn => {
        if (active) btn.classList.add('bx-files-active');
        else btn.classList.remove('bx-files-active');
      });
    },

    async loadProjects() {
      try {
        const res = await fetch('/projects');
        if (res.ok) {
          const list = await res.json();
          this.projects = Array.isArray(list) ? list : [];
        }
      } catch (e) {
        console.warn('[FilesCenter] fetch projects failed:', e);
      }
      try {
        const pRes = await fetch('/project');
        if (pRes.ok) {
          const p = await pRes.json();
          if (p && p.root_dir) {
            this.currentWorkspace = p.root_dir;
            this.currentProjectHash = p.project_hash || '';
          }
        }
      } catch (e) {
        console.warn('[FilesCenter] fetch active project failed:', e);
      }
    },

    async loadData(subOrAbsPath = '') {
      this.isLoading = true;
      this.currentPath = subOrAbsPath || this.currentWorkspace || '';
      try {
        const res = await fetch(`/fs/list?path=${encodeURIComponent(this.currentPath)}`);
        if (res.ok) {
          const data = await res.json();
          this.entries = data.entries || [];
          if (data.path) this.currentPath = data.path;
        } else {
          this.entries = [];
        }
      } catch (e) {
        console.warn('[FilesCenter] list fs failed:', e);
        this.entries = [];
      } finally {
        this.isLoading = false;
        this.render();
      }
    },

    async deleteItem(fullPath, isDir, name) {
      const targetType = isDir ? '文件夹及其内部所有内容' : '文件';
      const confirmed = window.confirm(`安全确认：\n\n确定要彻底删除该${targetType}吗？\n\n目标: "${name}"\n路径: ${fullPath}\n\n此操作不可撤销，请谨慎操作！`);
      if (!confirmed) return;

      try {
        const res = await fetch('/fs/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: fullPath })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast(`已成功删除: ${name}`);
        await this.loadData(this.currentPath);
      } catch (err) {
        alert(`删除失败: ${err.message}`);
      }
    },

    async createFolder() {
      const folderName = window.prompt('请输入要在当前目录下新建的子文件夹名称:');
      if (!folderName || !folderName.trim()) return;
      const name = folderName.trim();
      if (name.includes('/') || name.includes('\\')) {
        alert('文件夹名称不能包含斜杠');
        return;
      }
      const targetPath = this.currentPath ? `${this.currentPath}/${name}` : name;
      try {
        const res = await fetch('/fs/mkdir', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: targetPath })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        showToast(`已创建文件夹: ${name}`);
        await this.loadData(this.currentPath);
      } catch (err) {
        alert(`创建失败: ${err.message}`);
      }
    },

    formatTime(ts) {
      if (!ts) return '-';
      const d = new Date(ts);
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    },

    render() {
      const container = document.getElementById('bx-files-view-container');
      if (!container) return;

      const q = (this.searchQuery || '').toLowerCase().trim();
      const filtered = q
        ? (this.entries || []).filter(it => it.name.toLowerCase().includes(q))
        : (this.entries || []);

      const sorted = [...filtered].sort((a, b) => {
        if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

      const pathSegs = (this.currentPath || '').split('/').filter(Boolean);
      let crumbHtml = `<button type="button" class="bx-crumb-btn" data-p="/">${BxIcons.home(13)} 根目录</button>`;
      let acc = '';
      pathSegs.forEach((seg, idx) => {
        acc += '/' + seg;
        const isLast = (idx === pathSegs.length - 1);
        crumbHtml += `<span class="bx-crumb-sep">/</span>`;
        if (isLast) {
          crumbHtml += `<span class="bx-crumb-current">${escapeHtml(seg)}</span>`;
        } else {
          crumbHtml += `<button type="button" class="bx-crumb-btn" data-p="${escapeHtml(acc)}">${escapeHtml(seg)}</button>`;
        }
      });

      let projOptions = `<option value="">-- 选择切换工作区 --</option>`;
      (this.projects || []).forEach(p => {
        const pPath = p.root_dir || p.path || '';
        const pName = p.name || pPath.split('/').pop() || '未命名工作区';
        const isSel = (this.currentPath && this.currentPath.startsWith(pPath));
        projOptions += `<option value="${escapeHtml(pPath)}" ${isSel ? 'selected' : ''}>${escapeHtml(pName)} (${escapeHtml(pPath)})</option>`;
      });

      let parentRow = '';
      if (this.currentPath && this.currentPath !== '/' && pathSegs.length > 0) {
        const parentPath = '/' + pathSegs.slice(0, -1).join('/');
        parentRow = `
          <tr class="bx-file-row bx-fc-parent-row" data-p="${escapeHtml(parentPath || '/')}">
            <td colspan="5" style="cursor: pointer; padding: 10px 14px; font-weight: 600; color: #0066cc;">
              ${BxIcons.levelUp(14)} <strong>.. (返回上级目录)</strong>
            </td>
          </tr>
        `;
      }

      let rowsHtml = '';
      if (this.isLoading) {
        rowsHtml = `<tr><td colspan="5" style="text-align: center; padding: 40px; color: var(--bx-text-muted);">正在读取目录内容...</td></tr>`;
      } else if (sorted.length === 0) {
        rowsHtml = `<tr><td colspan="5" style="text-align: center; padding: 40px; color: var(--bx-text-muted);">暂无文件或目录</td></tr>`;
      } else {
        sorted.forEach(it => {
          const { icon, type } = getFileIconAndType(it.name, it.is_dir);
          const fullPath = it.full || `${this.currentPath}/${it.name}`;
          const sizeStr = it.is_dir ? '-' : formatFileSize(it.size);
          const timeStr = this.formatTime(it.mtime);

          rowsHtml += `
            <tr class="bx-file-row" data-full="${escapeHtml(fullPath)}" data-name="${escapeHtml(it.name)}" data-is-dir="${it.is_dir ? '1' : '0'}">
              <td class="bx-file-name-cell">
                <span class="bx-file-icon">${icon}</span>
                <span class="bx-file-title ${it.is_dir ? 'bx-dir-link' : ''}" title="${escapeHtml(it.name)}">${escapeHtml(it.name)}</span>
              </td>
              <td class="bx-file-type-cell">${type}</td>
              <td class="bx-file-size-cell">${sizeStr}</td>
              <td class="bx-file-time-cell" style="font-size: 0.76rem; color: var(--bx-text-muted);">${timeStr}</td>
              <td class="bx-file-ops-cell">
                <div class="bx-file-ops">
                  <button type="button" class="bx-file-action-btn bx-fc-finder-btn" title="在 Finder 中显示此${it.is_dir ? '文件夹' : '文件'}">${BxIcons.finder(12)} 访达</button>
                  <button type="button" class="bx-file-action-btn bx-fc-open-btn" title="在本地 IDE 中打开">${BxIcons.ide(12)} IDE</button>
                  <button type="button" class="bx-file-action-btn danger bx-fc-del-btn" title="删除该${it.is_dir ? '文件夹及全部内容' : '文件'}">${BxIcons.trash(12)} 删除</button>
                </div>
              </td>
            </tr>
          `;
        });
      }

      container.innerHTML = `
        <div class="bx-fc-header">
          <div class="bx-fc-header-left">
            <span style="display: flex; align-items: center; color: var(--bx-accent);">${BxIcons.files(22)}</span>
            <div>
              <div style="font-weight: 700; font-size: 1.15rem; display: flex; align-items: center; gap: 8px;">
                <span>工作区文件治理中心</span>
                <span class="bx-badge bx-badge-green" style="font-size: 0.68rem;">● 实时文件系统</span>
              </div>
              <div style="font-size: 0.74rem; color: var(--bx-text-muted);">
                自由穿透浏览 · 快速过滤 · IDE 协同联动 · 彻底物理删除
              </div>
            </div>
          </div>
          <div class="bx-fc-header-right">
            <select id="bx-fc-proj-select" class="bx-proj-select" style="font-size: 0.8rem; padding: 4px 10px; max-width: 320px;">
              ${projOptions}
            </select>
            <button type="button" class="bx-action-btn" id="bx-fc-close-btn" style="border-radius: 8px;">✕ 返回对话</button>
          </div>
        </div>

        <div class="bx-fc-toolbar">
          <div class="bx-files-breadcrumb-bar" id="bx-fc-crumbs-bar">
            ${crumbHtml}
          </div>
          <div style="display: flex; align-items: center; gap: 8px; flex-shrink: 0;">
            <input type="text" id="bx-fc-search-inp" class="bx-proj-select" style="width: 220px; font-size: 0.78rem;" placeholder="快速过滤文件名..." value="${escapeHtml(this.searchQuery)}" />
            <button type="button" class="bx-action-btn" id="bx-fc-refresh-btn" title="刷新列表">${BxIcons.refresh(13)} 刷新</button>
            <button type="button" class="bx-action-btn primary" id="bx-fc-mkdir-btn" title="新建子文件夹">${BxIcons.folderPlus(13)} 新建文件夹</button>
          </div>
        </div>

        <div class="bx-fc-content">
          <div class="bx-files-container" style="max-height: none; height: 100%; border-radius: 10px;">
            <table class="bx-files-table">
              <thead>
                <tr>
                  <th style="width: 42%;">文件 / 目录名称</th>
                  <th style="width: 16%;">类型</th>
                  <th style="width: 14%;">大小</th>
                  <th style="width: 16%;">修改时间</th>
                  <th style="width: 12%; text-align: right;">操作</th>
                </tr>
              </thead>
              <tbody>
                ${parentRow}
                ${rowsHtml}
              </tbody>
            </table>
          </div>
        </div>
      `;

      const closeBtn = container.querySelector('#bx-fc-close-btn');
      if (closeBtn) closeBtn.onclick = () => this.close();

      const refreshBtn = container.querySelector('#bx-fc-refresh-btn');
      if (refreshBtn) refreshBtn.onclick = () => this.loadData(this.currentPath);

      const mkdirBtn = container.querySelector('#bx-fc-mkdir-btn');
      if (mkdirBtn) mkdirBtn.onclick = () => this.createFolder();

      const searchInp = container.querySelector('#bx-fc-search-inp');
      if (searchInp) {
        searchInp.oninput = (e) => {
          this.searchQuery = e.target.value;
          this.render();
        };
      }

      const projSelect = container.querySelector('#bx-fc-proj-select');
      if (projSelect) {
        projSelect.onchange = (e) => {
          if (e.target.value) {
            this.loadData(e.target.value);
          }
        };
      }

      container.querySelectorAll('.bx-crumb-btn').forEach(btn => {
        btn.onclick = () => {
          const p = btn.getAttribute('data-p');
          if (p) this.loadData(p);
        };
      });

      const pRow = container.querySelector('.bx-fc-parent-row');
      if (pRow) {
        pRow.onclick = () => {
          const p = pRow.getAttribute('data-p');
          if (p) this.loadData(p);
        };
      }

      container.querySelectorAll('.bx-file-row:not(.bx-fc-parent-row)').forEach(row => {
        const full = row.getAttribute('data-full');
        const name = row.getAttribute('data-name');
        const isDir = row.getAttribute('data-is-dir') === '1';

        if (isDir) {
          const dirLink = row.querySelector('.bx-dir-link');
          if (dirLink) {
            dirLink.onclick = (e) => {
              e.stopPropagation();
              this.loadData(full);
            };
          }
          row.ondblclick = () => this.loadData(full);
        }

        const finderBtn = row.querySelector('.bx-fc-finder-btn');
        if (finderBtn) {
          finderBtn.onclick = async (e) => {
            e.stopPropagation();
            try {
              const res = await fetch('/api/ide/open', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ide_id: 'finder', file: full, working_dir: this.currentPath })
              });
              const data = await res.json();
              if (res.ok && data.ok) {
                showToast(`✓ 已在 Finder 中显示 ${name}`);
              } else {
                showToast(`打开 Finder 失败: ${data.error || '未知错误'}`, true);
              }
            } catch (err) {
              showToast(`打开 Finder 失败: ${err.message}`, true);
            }
          };
        }

        const openBtn = row.querySelector('.bx-fc-open-btn');
        if (openBtn) {
          openBtn.onclick = (e) => {
            e.stopPropagation();
            openInSelectedIDE(full);
          };
        }

        const delBtn = row.querySelector('.bx-fc-del-btn');
        if (delBtn) {
          delBtn.onclick = (e) => {
            e.stopPropagation();
            this.deleteItem(full, isDir, name);
          };
        }
      });
    }
  };

  window.openSkillsCenter = () => SkillsCenter.open();
  window.closeSkillsCenter = () => SkillsCenter.close();
  window.openMcpCenter = () => McpCenter.open();
  window.closeMcpCenter = () => McpCenter.close();
  window.openFilesCenter = (p) => FilesCenter.open(p);
  window.closeFilesCenter = () => FilesCenter.close();

  // =========================================================================
  // Workspace Project Context Menu & Deletion System (三个点选项菜单 · 编辑 · 访达 · IDE · 删除)
  // =========================================================================
  const WorkspaceContextMenu = {
    menuEl: null,
    confirmEl: null,
    editModalEl: null,
    lastRightClickTime: 0,
    lastRightClickTarget: null,
    cachedIdes: null,
    inited: false,

    init() {
      if (this.inited) return;
      this.inited = true;

      // 1. Delegated contextmenu on document for workspace projects & files
      document.addEventListener('contextmenu', (e) => {
        // A. Check if clicking on sidebar project (or any child inside .sidebar-project / .sidebar-project-row)
        const projRow = e.target.closest('.sidebar-project-row') || e.target.closest('.sidebar-project');
        if (projRow) {
          e.preventDefault();
          e.stopPropagation();
          this.showProjectOptionsMenu(projRow, null, e.clientX, e.clientY);
          return;
        }

        // B. Check if clicking on file row in FilesCenter
        const fcRow = e.target.closest('.bx-file-row');
        if (fcRow) {
          e.preventDefault();
          e.stopPropagation();
          const full = fcRow.getAttribute('data-full');
          const name = fcRow.getAttribute('data-name');
          const isDir = fcRow.getAttribute('data-is-dir') === '1';
          if (full) {
            this.handleFileRightClick(full, name, isDir, e.clientX, e.clientY);
            return;
          }
        }
      }, true);

      // 2. Close menu on outside click, escape, or resize
      document.addEventListener('click', (e) => {
        if (this.menuEl && !this.menuEl.contains(e.target)) {
          this.closeMenu();
        }
      });
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          this.closeMenu();
          this.closeConfirm();
          this.closeEditModal();
        }
      });
      window.addEventListener('resize', () => this.closeMenu());
      window.addEventListener('scroll', () => this.closeMenu(), true);
    },

    async getInstalledIdes() {
      if (this.cachedIdes && this.cachedIdes.length > 0) return this.cachedIdes;
      try {
        const res = await fetch('/api/ide/list');
        if (res.ok) {
          const data = await res.json();
          const items = data.items || data.tools || [];
          this.cachedIdes = items.filter(it => it.installed && (it.category === 'editor' || it.category === 'ai_ide'));
        }
      } catch (e) {
        console.warn('Failed to load IDE list', e);
      }
      if (!this.cachedIdes || !this.cachedIdes.length) {
        this.cachedIdes = [
          { id: 'vscode', name: 'VS Code' },
          { id: 'idea', name: 'IntelliJ IDEA' },
          { id: 'cursor', name: 'Cursor' },
        ];
      }
      return this.cachedIdes;
    },

    async resolveProject(projEl) {
      const section = projEl.closest('.sidebar-project') || projEl;
      const pBtn = section.querySelector('.sidebar-project-btn');
      const workingDir = pBtn ? (pBtn.getAttribute('title') || '').trim() : '';
      const nameSpan = section.querySelector('.sidebar-project-name');
      let name = nameSpan ? nameSpan.textContent.trim() : '';
      if (!name && workingDir) {
        name = workingDir.split('/').filter(Boolean).pop() || '';
      }

      // Check projectsList cache or fetch from /projects
      let list = window.projectsList;
      if (!list || !list.length) {
        try {
          const res = await fetch('/projects');
          if (res.ok) {
            list = await res.json();
            window.projectsList = list;
          }
        } catch (e) {}
      }
      if (!list) list = [];

      let found = list.find(p => {
        if (workingDir && p.working_dir) {
          if (p.working_dir === workingDir) return true;
          if (p.working_dir.replace(/\/+$/, '') === workingDir.replace(/\/+$/, '')) return true;
        }
        return false;
      });
      if (!found && name) {
        found = list.find(p => p.name === name);
      }

      return {
        section,
        workingDir: (found && found.working_dir) || workingDir,
        name: (found && found.name) || name || '未命名项目',
        hash: (found && found.hash) || '',
        sessionCount: (found && found.session_count) || 0,
      };
    },

    async handleProjectRightClick(projEl, x, y, isDouble = false) {
      this.showProjectOptionsMenu(projEl, null, x, y);
    },

    async showProjectOptionsMenu(projRow, anchorEl, x, y) {
      this.closeMenu();
      const proj = await this.resolveProject(projRow);
      if (!proj) return;
      const ides = await this.getInstalledIdes();

      const pop = document.createElement('div');
      pop.className = 'bx-workspace-context-menu bx-project-options-popover';
      pop.setAttribute('role', 'menu');

      let ideGridHtml = '';
      if (ides && ides.length) {
        ideGridHtml = `
          <div class="bx-pop-ide-group">
            <div class="bx-pop-ide-label">
              <span>${BxIcons.ide(13)}</span>
              <span>使用本地 IDE 打开</span>
            </div>
            <div class="bx-pop-ide-grid">
              ${ides.map(ide => `
                <button type="button" class="bx-pop-ide-btn" data-ide-id="${ide.id}" title="在 ${escapeHtml(ide.name)} 中打开工作区">
                  <span class="bx-pop-ide-icon">${getIdeBrandIcon(ide.id)}</span>
                  <span class="bx-pop-ide-name">${escapeHtml(ide.name)}</span>
                </button>
              `).join('')}
            </div>
          </div>
        `;
      }

      pop.innerHTML = `
        <div class="bx-ctx-header">
          <span class="bx-ctx-header-icon" style="color: #007aff;">${BxIcons.folder(18)}</span>
          <div class="bx-ctx-header-info">
            <div class="bx-ctx-header-title">${escapeHtml(proj.name)}</div>
            <div class="bx-ctx-header-path" title="${escapeHtml(proj.workingDir)}">${escapeHtml(proj.workingDir)}</div>
          </div>
        </div>
        <div class="bx-ctx-divider"></div>
        <button type="button" class="bx-ctx-item" id="bx-pop-edit">
          <span class="bx-ctx-item-icon" style="color: var(--bx-brand-sky-deep);">${BxIcons.edit(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">编辑</span>
            <span class="bx-ctx-item-desc">修改工作区显示名称</span>
          </div>
        </button>
        <button type="button" class="bx-ctx-item" id="bx-pop-settings">
          <span class="bx-ctx-item-icon" style="color: var(--bx-brand-sky);">${BxIcons.settings(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">项目全景配置</span>
            <span class="bx-ctx-item-desc">五大安全门禁、挖掘监控与模型参数</span>
          </div>
        </button>
        <button type="button" class="bx-ctx-item" id="bx-pop-finder">
          <span class="bx-ctx-item-icon" style="color: #007aff;">${BxIcons.finder(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">在 Finder 显示</span>
            <span class="bx-ctx-item-desc">在 macOS 访达中查看此工作区</span>
          </div>
        </button>
        <div class="bx-ctx-divider"></div>
        ${ideGridHtml}
        <div class="bx-ctx-divider"></div>
        <button type="button" class="bx-ctx-item" id="bx-pop-new-session">
          <span class="bx-ctx-item-icon" style="color: #10b981;">${BxIcons.plus(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">在此工作区新建对话</span>
          </div>
        </button>
        <button type="button" class="bx-ctx-item" id="bx-pop-files">
          <span class="bx-ctx-item-icon" style="color: #6366f1;">${BxIcons.files(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">工作区文件治理</span>
          </div>
        </button>
        <div class="bx-ctx-divider"></div>
        <button type="button" class="bx-ctx-item danger" id="bx-pop-delete">
          <span class="bx-ctx-item-icon">${BxIcons.trash(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">从工作区删除</span>
            <span class="bx-ctx-item-desc">从列表移除，不再显示此项目</span>
          </div>
          <span class="bx-ctx-badge">移除</span>
        </button>
      `;

      this.closeMenu();
      document.body.appendChild(pop);
      this.menuEl = pop;

      // Position popover
      const popWidth = 280;
      const popHeight = 360;
      let left = x || 100;
      let top = y || 100;
      if (anchorEl) {
        const rect = anchorEl.getBoundingClientRect();
        left = rect.right + 6;
        top = rect.top - 4;
        if (left + popWidth > window.innerWidth - 10) {
          left = rect.left - popWidth - 6;
        }
      } else {
        if (left + popWidth > window.innerWidth - 10) {
          left = window.innerWidth - popWidth - 10;
        }
      }
      if (top + popHeight > window.innerHeight - 10) {
        top = Math.max(10, window.innerHeight - popHeight - 10);
      }
      pop.style.left = `${Math.max(10, left)}px`;
      pop.style.top = `${Math.max(10, top)}px`;

      // 1. Edit
      pop.querySelector('#bx-pop-edit').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        this.openEditProjectModal(proj);
      };

      // 1.1 Project Settings (紧随编辑下方，直达五大安全门禁与模型配置)
      pop.querySelector('#bx-pop-settings').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        if (typeof openSettingsModal === 'function') {
          openSettingsModal(proj.hash);
        }
      };

      // 2. Finder
      pop.querySelector('#bx-pop-finder').onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        try {
          const res = await fetch('/api/ide/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ide_id: 'finder', working_dir: proj.workingDir, project_hash: proj.hash })
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            showToast(`✓ 已在 Finder 中显示「${proj.name}」`);
          } else {
            showToast(`打开 Finder 失败: ${data.error || '未知错误'}`, true);
          }
        } catch (err) {
          showToast(`打开 Finder 失败: ${err.message}`, true);
        }
      };

      // 3. IDE buttons
      pop.querySelectorAll('.bx-pop-ide-btn').forEach(btn => {
        btn.onclick = async (e) => {
          e.preventDefault();
          e.stopPropagation();
          const ideId = btn.getAttribute('data-ide-id');
          const ideName = btn.querySelector('.bx-pop-ide-name')?.textContent || ideId;
          this.closeMenu();
          try {
            const res = await fetch('/api/ide/open', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ ide_id: ideId, working_dir: proj.workingDir, project_hash: proj.hash })
            });
            const data = await res.json();
            if (res.ok && data.ok) {
              showToast(`已在 ${ideName} 中打开「${proj.name}」`);
            } else {
              showToast(`打开 ${ideName} 失败: ${data.error || '未知错误'}`, true);
            }
          } catch (err) {
            showToast(`打开 ${ideName} 失败: ${err.message}`, true);
          }
        };
      });

      // 4. New session
      pop.querySelector('#bx-pop-new-session').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        fetch('/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ working_dir: proj.workingDir })
        }).then(r => r.json()).then(data => {
          if (data && data.id) {
            showToast(`✓ 已在「${proj.name}」创建新对话`);
            location.reload();
          }
        }).catch(() => {});
      };

      // 5. Files
      pop.querySelector('#bx-pop-files').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        if (typeof FilesCenter !== 'undefined') {
          FilesCenter.open(proj.workingDir);
        }
      };

      // 6. Delete
      pop.querySelector('#bx-pop-delete').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        this.confirmDeleteProject(proj);
      };
    },

    openEditProjectModal(proj) {
      this.closeConfirm();
      this.closeEditModal();
      const backdrop = document.createElement('div');
      backdrop.className = 'bx-modal-backdrop bx-edit-proj-backdrop';
      backdrop.innerHTML = `
        <div class="bx-edit-proj-modal">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px;">
            <div style="display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 1.05rem; color: var(--bx-text-main);">
              <span style="color: var(--bx-brand-sky-deep);">${BxIcons.edit(18)}</span>
              <span>编辑工作区名称</span>
            </div>
            <button type="button" class="bx-action-btn bx-edit-close" style="padding: 4px 8px; border-radius: 6px;">${BxIcons.close(13)}</button>
          </div>

          <div style="margin-bottom: 14px;">
            <label style="display: block; font-size: 0.78rem; font-weight: 600; color: var(--bx-text-muted); margin-bottom: 5px;">工作区显示名称</label>
            <input type="text" id="bx-edit-name-inp" class="bx-proj-select" style="width: 100%; font-size: 0.88rem; padding: 7px 10px; border-radius: 8px;" value="${escapeHtml(proj.name)}" placeholder="请输入工作区名称" />
          </div>

          <div style="margin-bottom: 18px;">
            <label style="display: block; font-size: 0.78rem; font-weight: 600; color: var(--bx-text-muted); margin-bottom: 5px;">物理路径</label>
            <div style="font-size: 0.74rem; font-family: monospace; background: var(--bx-card-bg, rgba(0,0,0,0.03)); padding: 8px 10px; border-radius: 6px; border: 1px solid var(--bx-modal-border, rgba(0,0,0,0.08)); word-break: break-all; color: var(--bx-text-main);" title="${escapeHtml(proj.workingDir)}">
              ${escapeHtml(proj.workingDir)}
            </div>
          </div>

          <div style="display: flex; justify-content: flex-end; gap: 8px;">
            <button type="button" class="bx-action-btn" id="bx-edit-cancel" style="border-radius: 8px;">取消</button>
            <button type="button" class="bx-action-btn primary" id="bx-edit-save" style="border-radius: 8px;">${BxIcons.check(13)} 保存名称</button>
          </div>
        </div>
      `;

      document.body.appendChild(backdrop);
      this.editModalEl = backdrop;

      const inp = backdrop.querySelector('#bx-edit-name-inp');
      if (inp) {
        inp.focus();
        inp.select();
      }

      const closeSelf = () => this.closeEditModal();
      backdrop.querySelector('.bx-edit-close').onclick = closeSelf;
      backdrop.querySelector('#bx-edit-cancel').onclick = closeSelf;
      backdrop.onclick = (e) => {
        if (e.target === backdrop) closeSelf();
      };

      backdrop.querySelector('#bx-edit-save').onclick = async () => {
        const newName = (inp.value || '').trim();
        if (!newName) {
          showToast('项目名称不能为空', true);
          return;
        }
        try {
          const endpoint = proj.hash ? `/projects/${encodeURIComponent(proj.hash)}/rename` : '/projects/rename';
          const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName, working_dir: proj.workingDir, project_hash: proj.hash })
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            if (proj.section) {
              const nameSpan = proj.section.querySelector('.sidebar-project-name');
              if (nameSpan) nameSpan.textContent = newName;
            }
            if (window.projectsList && Array.isArray(window.projectsList)) {
              const p = window.projectsList.find(x => x.hash === proj.hash || x.working_dir === proj.workingDir);
              if (p) p.name = newName;
            }
            showToast(`✓ 已将工作区重命名为: ${newName}`);
            closeSelf();
          } else {
            showToast(`重命名失败: ${data.error || '未知错误'}`, true);
          }
        } catch (err) {
          showToast(`重命名失败: ${err.message}`, true);
        }
      };
    },

    handleFileRightClick(full, name, isDir, x, y) {
      this.closeMenu();
      const menu = document.createElement('div');
      menu.className = 'bx-workspace-context-menu';
      menu.innerHTML = `
        <div class="bx-ctx-header">
          <span class="bx-ctx-header-icon" style="color: #007aff;">${isDir ? BxIcons.folder(18) : BxIcons.file(18)}</span>
          <div class="bx-ctx-header-info">
            <div class="bx-ctx-header-title">${escapeHtml(name)}</div>
            <div class="bx-ctx-header-path" title="${escapeHtml(full)}">${escapeHtml(full)}</div>
          </div>
        </div>
        <div class="bx-ctx-divider"></div>
        <button type="button" class="bx-ctx-item" id="bx-ctx-file-finder">
          <span class="bx-ctx-item-icon" style="color: #007aff;">${BxIcons.finder(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">在 Finder 显示</span>
            <span class="bx-ctx-item-desc">在 macOS 访达中查看与定位</span>
          </div>
        </button>
        <button type="button" class="bx-ctx-item" id="bx-ctx-file-ide">
          <span class="bx-ctx-item-icon" style="color: #0284c7;">${BxIcons.ide(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">在编辑器中打开</span>
            <span class="bx-ctx-item-desc">在本地 IDE 打开此${isDir ? '文件夹' : '文件'}</span>
          </div>
        </button>
        <button type="button" class="bx-ctx-item" id="bx-ctx-file-copy">
          <span class="bx-ctx-item-icon">${BxIcons.copy(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">复制绝对路径</span>
          </div>
        </button>
        <div class="bx-ctx-divider"></div>
        <button type="button" class="bx-ctx-item danger" id="bx-ctx-file-delete">
          <span class="bx-ctx-item-icon">${BxIcons.trash(15)}</span>
          <div class="bx-ctx-item-content">
            <span class="bx-ctx-item-title">彻底物理删除</span>
            <span class="bx-ctx-item-desc">从磁盘安全销毁此${isDir ? '文件夹' : '文件'}</span>
          </div>
          <span class="bx-ctx-badge">删除</span>
        </button>
      `;

      document.body.appendChild(menu);
      this.menuEl = menu;

      let left = x;
      let top = y;
      if (left + 260 > window.innerWidth - 10) left = window.innerWidth - 270;
      if (top + 210 > window.innerHeight - 10) top = Math.max(10, y - 210);
      menu.style.left = `${Math.max(10, left)}px`;
      menu.style.top = `${Math.max(10, top)}px`;

      menu.querySelector('#bx-ctx-file-finder').onclick = async (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        try {
          const res = await fetch('/api/ide/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ide_id: 'finder', file: full })
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            showToast(`✓ 已在 Finder 中显示 ${name}`);
          }
        } catch (err) {}
      };

      menu.querySelector('#bx-ctx-file-ide').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        if (typeof openInSelectedIDE === 'function') {
          openInSelectedIDE(full);
        }
      };

      menu.querySelector('#bx-ctx-file-copy').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        navigator.clipboard.writeText(full).then(() => {
          showToast('✓ 已复制路径至剪贴板');
        }).catch(() => {
          showToast('复制路径失败', true);
        });
      };

      menu.querySelector('#bx-ctx-file-delete').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeMenu();
        if (typeof FilesCenter !== 'undefined') {
          FilesCenter.deleteItem(full, isDir, name);
        }
      };
    },

    enhanceProjectRows() {
      // 1. 彻底清除任何行内暴露的快捷垃圾桶按钮 (图片 4/5 修复)
      document.querySelectorAll('.bx-proj-del-quick-btn').forEach(b => b.remove());
      document.querySelectorAll('.bx-proj-files-quick-btn').forEach(b => b.remove());

      // 2. 将加号按钮 (.sidebar-project-new) 坚决转换为极简三点操作图标 (···)
      const rows = document.querySelectorAll('.sidebar-project-row');
      rows.forEach(row => {
        const newBtn = row.querySelector('.sidebar-project-new');
        if (newBtn) {
          if (!newBtn.classList.contains('bx-proj-more-btn')) {
            newBtn.classList.add('bx-proj-more-btn');
          }
          newBtn.title = '工作区选项 (编辑 · 在 Finder 显示 · 使用 IDE 打开 · 删除)';
          newBtn.setAttribute('aria-label', '工作区选项');
          // 确保加号 SVG 被彻底替换为三点图标
          if (!newBtn.querySelector('svg.bx-icon') || newBtn.innerHTML.includes('line') || newBtn.innerHTML.includes('path d="M8 3.5') || newBtn.innerHTML.includes('path d="M8 3')) {
            newBtn.innerHTML = BxIcons.moreHorizontal(14);
          }

          if (!newBtn._bxBound) {
            newBtn._bxBound = true;
            newBtn.addEventListener('click', (e) => {
              e.preventDefault();
              e.stopPropagation();
              e.stopImmediatePropagation();
              WorkspaceContextMenu.showProjectOptionsMenu(row, newBtn, e.clientX, e.clientY);
            }, true);
          }
        }
      });
    },

    confirmDeleteProject(proj) {
      this.closeConfirm();
      const backdrop = document.createElement('div');
      backdrop.className = 'bx-confirm-dialog-backdrop';
      backdrop.innerHTML = `
        <div class="bx-confirm-dialog">
          <div class="bx-confirm-icon-wrap danger">
            ${BxIcons.trash(26)}
          </div>
          <div class="bx-confirm-content">
            <h3 class="bx-confirm-title">从工作区移除项目？</h3>
            <div class="bx-confirm-desc">
              即将从工作区移除项目 <strong>「${escapeHtml(proj.name)}」</strong>。
              <div class="bx-confirm-note">
                <span class="bx-confirm-note-icon">${BxIcons.shield(14)}</span>
                <div>
                  <strong>安全保障</strong>：仅将该项目从工作区列表中移除，并不再显示。磁盘上的源代码和业务工程文件<strong>绝对保留</strong>，不会被物理删除。
                </div>
              </div>
            </div>
          </div>
          <div class="bx-confirm-footer">
            <button type="button" class="bx-confirm-btn cancel" id="bx-confirm-cancel">取消</button>
            <button type="button" class="bx-confirm-btn danger" id="bx-confirm-ok">确认删除 (不再显示)</button>
          </div>
        </div>
      `;

      document.body.appendChild(backdrop);
      this.confirmEl = backdrop;

      const cancelBtn = backdrop.querySelector('#bx-confirm-cancel');
      cancelBtn.onclick = () => this.closeConfirm();

      const okBtn = backdrop.querySelector('#bx-confirm-ok');
      okBtn.onclick = async () => {
        okBtn.disabled = true;
        okBtn.textContent = '正在删除...';
        await this.executeDelete(proj);
      };

      backdrop.onclick = (e) => {
        if (e.target === backdrop) this.closeConfirm();
      };
    },

    async executeDelete(proj) {
      try {
        let endpoint = proj.hash ? `/projects/${encodeURIComponent(proj.hash)}?mode=remove` : `/projects/delete`;
        let options = { method: proj.hash ? 'DELETE' : 'POST' };
        if (!proj.hash) {
          options.headers = { 'Content-Type': 'application/json' };
          options.body = JSON.stringify({ working_dir: proj.workingDir, mode: 'remove' });
        }

        let res = await fetch(endpoint, options);
        let data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
          res = await fetch('/projects/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hash: proj.hash, working_dir: proj.workingDir, mode: 'remove' }),
          });
          data = await res.json().catch(() => ({}));
        }

        if (!res.ok && data.error) {
          throw new Error(data.error || '删除请求失败');
        }

        // Smoothly animate out and remove from DOM
        if (proj.section) {
          proj.section.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
          proj.section.style.opacity = '0';
          proj.section.style.transform = 'translateX(-24px) scale(0.96)';
          proj.section.style.maxHeight = `${proj.section.scrollHeight}px`;
          requestAnimationFrame(() => {
            proj.section.style.maxHeight = '0px';
            proj.section.style.marginTop = '0px';
            proj.section.style.marginBottom = '0px';
            proj.section.style.paddingTop = '0px';
            proj.section.style.paddingBottom = '0px';
            proj.section.style.overflow = 'hidden';
          });
          setTimeout(() => {
            proj.section.remove();
          }, 320);
        }

        // Update memory project list
        if (window.projectsList && Array.isArray(window.projectsList)) {
          window.projectsList = window.projectsList.filter(p => p.hash !== proj.hash && p.working_dir !== proj.workingDir);
        }

        this.closeConfirm();
        showToast(`✓ 已将「${proj.name}」从工作区删除 (不再显示)`);

        if (typeof FilesCenter !== 'undefined' && FilesCenter.container) {
          FilesCenter.loadProjects();
        }
      } catch (err) {
        showToast(`删除失败: ${err.message}`, true);
        this.closeConfirm();
      }
    },

    closeMenu() {
      document.querySelectorAll('.bx-workspace-context-menu').forEach(el => el.remove());
      this.menuEl = null;
    },

    closeConfirm() {
      document.querySelectorAll('.bx-confirm-dialog-backdrop').forEach(el => el.remove());
      this.confirmEl = null;
    },

    closeEditModal() {
      if (this.editModalEl) {
        this.editModalEl.remove();
        this.editModalEl = null;
      }
      document.querySelectorAll('.bx-edit-proj-backdrop').forEach(el => el.remove());
    }
  };

  window.WorkspaceContextMenu = WorkspaceContextMenu;

  function interceptSkillsSidebarClick() {
    const actions = document.querySelectorAll('.sidebar-action');
    actions.forEach(btn => {
      const label = btn.querySelector('.sidebar-action-label');
      if (label && label.textContent.trim() === '技能') {
        btn.setAttribute('data-bx-skills-tab', '1');
        btn.removeAttribute('aria-haspopup');
        const caret = btn.querySelector('.sidebar-action-caret');
        if (caret) caret.style.display = 'none';

        if (!btn.dataset.bxIntercepted) {
          btn.dataset.bxIntercepted = '1';
          btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const pop = document.querySelector('.item-menu.skills-menu, .item-menu');
            if (pop) pop.remove();
            if (typeof McpCenter !== 'undefined' && McpCenter.isOpen) McpCenter.close();
            SkillsCenter.toggle();
          }, true);
        }
      } else if (label && label.textContent.trim() === 'MCP') {
        btn.setAttribute('data-bx-mcp-tab', '1');
        btn.removeAttribute('aria-haspopup');
        const caret = btn.querySelector('.sidebar-action-caret');
        if (caret) caret.style.display = 'none';

        if (!btn.dataset.bxMcpIntercepted) {
          btn.dataset.bxMcpIntercepted = '1';
          btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const pop = document.querySelector('.item-menu.mcp-menu, .item-menu');
            if (pop) pop.remove();
            if (typeof SkillsCenter !== 'undefined' && SkillsCenter.isOpen) SkillsCenter.close();
            McpCenter.toggle();
          }, true);
        }
      } else if (label && label.textContent.trim() === '新建对话') {
        if (!btn.dataset.bxCloseSkills) {
          btn.dataset.bxCloseSkills = '1';
          btn.addEventListener('click', () => {
            SkillsCenter.close();
            if (typeof McpCenter !== 'undefined') McpCenter.close();
            if (typeof FilesCenter !== 'undefined') FilesCenter.close();
          });
        }
      }
    });

    // 1. Inject "工作区文件" into .sidebar-action list
    let filesBtn = document.querySelector('.sidebar-action.bx-files-sidebar-action');
    if (!filesBtn) {
      const allActions = Array.from(document.querySelectorAll('.sidebar-action'));
      const mcpBtn = allActions.find(b => {
        const l = b.querySelector('.sidebar-action-label');
        return l && l.textContent.trim() === 'MCP';
      });
      if (mcpBtn && mcpBtn.parentNode) {
        filesBtn = document.createElement('button');
        filesBtn.className = 'sidebar-action bx-files-sidebar-action';
        filesBtn.title = '工作区文件治理与安全删除';
        filesBtn.innerHTML = `
          <span class="sidebar-action-icon">${BxIcons.files(18)}</span>
          <span class="sidebar-action-label">工作区文件</span>
        `;
        filesBtn.onclick = (e) => {
          e.preventDefault();
          e.stopPropagation();
          FilesCenter.toggle();
        };
        mcpBtn.parentNode.insertBefore(filesBtn, mcpBtn.nextSibling);
      }
    }

    // 2. Clean up any obsolete duplicate quick buttons
    document.querySelectorAll('.bx-group-files-btn').forEach(b => b.remove());

    // 3. Enhance workspace project rows with three dots (···) and popover options
    WorkspaceContextMenu.init();
    WorkspaceContextMenu.enhanceProjectRows();

    const railBtns = document.querySelectorAll('.rail-btn');
    railBtns.forEach(btn => {
      if (btn.title === '技能' || btn.getAttribute('aria-label') === '技能') {
        if (!btn.dataset.bxIntercepted) {
          btn.dataset.bxIntercepted = '1';
          btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const pop = document.querySelector('.item-menu.skills-menu, .item-menu');
            if (pop) pop.remove();
            if (typeof McpCenter !== 'undefined' && McpCenter.isOpen) McpCenter.close();
            SkillsCenter.toggle();
          }, true);
        }
      } else if (btn.title === 'MCP' || btn.getAttribute('aria-label') === 'MCP') {
        if (!btn.dataset.bxMcpIntercepted) {
          btn.dataset.bxMcpIntercepted = '1';
          btn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
            const pop = document.querySelector('.item-menu.mcp-menu, .item-menu');
            if (pop) pop.remove();
            if (typeof SkillsCenter !== 'undefined' && SkillsCenter.isOpen) SkillsCenter.close();
            McpCenter.toggle();
          }, true);
        }
      }
    });

    const sessionList = document.querySelector('.session-list');
    if (sessionList && !sessionList.dataset.bxCloseSkillsObs) {
      sessionList.dataset.bxCloseSkillsObs = '1';
      sessionList.addEventListener('click', (e) => {
        const item = e.target.closest('.session-item, .session-item-main');
        if (item) {
          SkillsCenter.close();
          if (typeof McpCenter !== 'undefined') McpCenter.close();
          if (typeof FilesCenter !== 'undefined') FilesCenter.close();
        }
      });
    }
  }

  async function initComposerHostAndModelEnhancer() {
    try {
      const res = await fetch('/models');
      if (res.ok) {
        const list = await res.json();
        if (Array.isArray(list) && list.length) {
          composerCatalog = list;
          const def = list.find(x => x.is_default) || list[0];
          if (def && def.provider) selectedHostProvider = def.provider;
          list.forEach(item => {
            if (item.provider && item.model_id && !selectedHostModels[item.provider]) {
              selectedHostModels[item.provider] = item.model_id;
            }
          });
        }
      }
    } catch (e) {
      console.warn('[ComposerEnhancer] fetch /models failed:', e);
    }

    let enhanceScheduled = false;
    const runEnhance = () => {
      enhanceScheduled = false;
      decorateHostSelectorIcons();
      renderComposerModelPill();
      enhanceCwdModal();
      enhanceDropdownMenus();
      interceptSkillsSidebarClick();
      SkillsCenter.init();
      McpCenter.init();
      FilesCenter.init();
      scrubElementCitations(document.body);
    };
    runEnhance();
    const obs = new MutationObserver(() => {
      if (enhanceScheduled) return;
      enhanceScheduled = true;
      requestAnimationFrame(runEnhance);
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  // =========================================================================
  // Citation & Grounding Token Sanitizer for Live DOM
  // =========================================================================
  const CITATION_REGEX = /\ue200[^\ue201]*\ue201|[\ue200-\ue20f]|\bcite_?turn\d+[a-z0-9_-]*\b|\bturn\d+(?:forecast|search|source|news|docs|web|product|result|tool|ref|page)\d*\b|【\s*\d+(?:[:†]\d+)?(?:†[a-z0-9_-]+)?\s*】|\[\s*(?:cite|citation):\s*[^\]]+\]/gi;

  function scrubElementCitations(node) {
    if (!node) return;
    if (node.nodeType === Node.TEXT_NODE) {
      if (CITATION_REGEX.test(node.nodeValue)) {
        node.nodeValue = node.nodeValue.replace(CITATION_REGEX, '').replace(/ +([，。！？,.!?;:])/g, '$1');
      }
      return;
    }
    if (node.nodeType === Node.ELEMENT_NODE) {
      const tag = node.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'INPUT' || tag === 'TEXTAREA') return;
      for (let child of node.childNodes) {
        scrubElementCitations(child);
      }
    }
  }

  function initCitationSanitizer() {
    scrubElementCitations(document.body);
    removeUnwantedTriggers();
    const observer = new MutationObserver((mutations) => {
      let shouldClean = false;
      for (const m of mutations) {
        if (m.type === 'characterData') {
          scrubElementCitations(m.target);
        } else if (m.type === 'childList') {
          m.addedNodes.forEach(node => scrubElementCitations(node));
          shouldClean = true;
        }
      }
      if (shouldClean) {
        removeUnwantedTriggers();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  }

  let _themeSyncInitialized = false;
  let _isSyncingTheme = false;

  function syncThemeClasses() {
    if (_isSyncingTheme) return;
    _isSyncingTheme = true;
    try {
      const root = document.documentElement;
      const themeAttr = root.getAttribute('data-theme') || (typeof localStorage !== 'undefined' ? localStorage.getItem('atomcode.theme') : null) || 'light';
      const systemDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      const isDark = themeAttr === 'dark' || (themeAttr === 'system' && systemDark);

      if (isDark) {
        if (!root.classList.contains('dark')) root.classList.add('dark');
        if (root.classList.contains('light')) root.classList.remove('light');
        if (document.body) {
          if (!document.body.classList.contains('dark')) document.body.classList.add('dark');
          if (document.body.classList.contains('light')) document.body.classList.remove('light');
        }
      } else {
        if (root.classList.contains('dark')) root.classList.remove('dark');
        if (!root.classList.contains('light')) root.classList.add('light');
        if (document.body) {
          if (document.body.classList.contains('dark')) document.body.classList.remove('dark');
          if (!document.body.classList.contains('light')) document.body.classList.add('light');
        }
      }
    } catch (e) {
      console.warn('[BugX Studio] Theme sync error:', e);
    } finally {
      _isSyncingTheme = false;
    }
  }

  function initThemeSynchronizer() {
    if (_themeSyncInitialized) {
      syncThemeClasses();
      return;
    }
    _themeSyncInitialized = true;
    syncThemeClasses();

    try {
      const observer = new MutationObserver((mutations) => {
        for (const m of mutations) {
          if (m.type === 'attributes' && m.attributeName === 'data-theme') {
            syncThemeClasses();
          }
        }
      });
      observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    } catch (e) {}

    try {
      if (window.matchMedia) {
        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
        const listener = () => syncThemeClasses();
        if (mediaQuery.addEventListener) {
          mediaQuery.addEventListener('change', listener);
        } else if (mediaQuery.addListener) {
          mediaQuery.addListener(listener);
        }
      }
    } catch (e) {}
  }

  window.syncThemeClasses = syncThemeClasses;
  initThemeSynchronizer();

  // Initialize after DOM loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      initThemeSynchronizer();
      removeUnwantedTriggers();
      initComposerHostAndModelEnhancer();
      initCitationSanitizer();
      WorkspaceContextMenu.init();
    });
  } else {
    initThemeSynchronizer();
    removeUnwantedTriggers();
    initComposerHostAndModelEnhancer();
    initCitationSanitizer();
    WorkspaceContextMenu.init();
  }
})();
