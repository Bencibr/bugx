/*
 * Built DSH browser bundle. DSH loads client bundles through its lazy-CJS
 * ModuleLoader contract, so this file intentionally has no Node or network
 * dependencies. The mark is kept as inline SVG because the browser bundle is
 * served independently from the package's binary assets.
 */
window.__ModuleLoader__.load({
  id: '@bencibro/bugx-dsh-ui',
  factory: (require) => {
    const module = { exports: {} };
    const exports = module.exports;
    const React = require('react');
    const h = React.createElement;
    const BUGX_ID = '@bencibro/bugx-dsh-ui';

    function BugxIcon({ size = 24, className }) {
      return h(
        'svg',
        {
          viewBox: '0 0 512 512',
          width: size,
          height: size,
          className,
          role: 'img',
          'aria-label': 'BugX',
          focusable: 'false',
        },
        h('path', {
          fill: '#36c5dc',
          d: 'M80 310c0-98 79-178 176-178s176 80 176 178c0 47-38 85-85 85-23 0-45-9-61-25l-30-30-30 30c-16 16-38 25-61 25-47 0-85-38-85-85Z',
        }),
        h('ellipse', { cx: 181, cy: 283, rx: 30, ry: 42, fill: '#050505' }),
        h('ellipse', { cx: 331, cy: 283, rx: 30, ry: 42, fill: '#050505' }),
      );
    }

    const definition = {
      id: BUGX_ID,
      kind: 'bugx',
      priority: 'extension',
      title: () => 'BugX',
      guide: [{
        order: 20,
        title: () => 'BugX',
        description: () => 'Evidence-driven bug discovery and verification',
        icon: BugxIcon,
      }],
    };

    function BugxBody() {
      return h(
        'div',
        {
          style: {
            display: 'grid',
            gap: '12px',
            padding: '20px',
            placeItems: 'center',
            textAlign: 'center',
          },
        },
        h(BugxIcon, { size: 72 }),
        h('strong', null, 'BugX'),
        h('span', null, 'Start an audit from the BugX Agent entry.'),
      );
    }

    exports.inject = ['slots', 'sidebarRightTabs'];
    exports.apply = (ctx) => {
      ctx.effect(
        () => ctx.sidebarRightTabs.register(definition),
        'bugx-dsh-ui: sidebar icon',
      );
      ctx.effect(
        () => ctx.slots.inject(
          'sidebar.right.pane.tab',
          () => ctx.slots.register(
            { name: 'sidebar.right.pane.tab', key: BUGX_ID },
            BugxBody,
          ),
        ),
        'bugx-dsh-ui: sidebar body',
      );
    };
    return module.exports;
  },
});
