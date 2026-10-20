import { defineConfig } from 'vitepress'

export default defineConfig({
  base: '/safekeep/',
  title: 'safekeep',
  description: 'Event-driven selective file backup for macOS and Linux',

  markdown: {
    lineNumbers: true
  },

  // VitePress 1.x only serializes `themeConfig` into the site data:
  // nav/sidebar/search/etc. at the top level are silently dropped.
  themeConfig: {
    nav: [
      {
        text: 'Guide',
        items: [
          { text: 'Quickstart', link: '/quickstart' },
          { text: 'Install', link: '/install' },
          { text: 'Configuration', link: '/configuration' },
          { text: 'Commands', link: '/commands' },
          { text: 'S3 destination', link: '/s3' }
        ]
      },
      { text: 'How it works', link: '/how-it-works' },
      { text: 'Troubleshooting', link: '/troubleshooting' }
    ],

    sidebar: [
      {
        text: 'Guide',
        items: [
          { text: 'Quickstart', link: '/quickstart' },
          { text: 'Install', link: '/install' },
          { text: 'Configuration', link: '/configuration' },
          { text: 'Commands', link: '/commands' },
          { text: 'S3 destination', link: '/s3' }
        ]
      },
      {
        text: 'Reference',
        items: [
          { text: 'How it works', link: '/how-it-works' },
          { text: 'Agent (launchd / systemd)', link: '/agent' },
          { text: 'Troubleshooting', link: '/troubleshooting' },
          { text: 'Development', link: '/development' }
        ]
      }
    ],

    search: {
      provider: 'local'
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/foxhound87/safekeep' },
      { icon: 'gitlab', link: 'https://gitlab.com/foxhound87/safekeep' }
    ],

    editLink: {
      pattern: 'https://github.com/foxhound87/safekeep/edit/docs/docs/:path',
      text: 'Edit this page on GitHub'
    },

    footer: {
      message: 'Released under the MIT License.',
      copyright: 'safekeep'
    }
  }
})
