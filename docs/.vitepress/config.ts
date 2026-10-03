import { defineConfig } from 'vitepress'

export default defineConfig({
  base: '/safekeep/',
  title: 'safekeep',
  description: 'Event-driven selective file backup for macOS',

  markdown: {
    lineNumbers: true
  },

  nav: [
    {
      text: 'Guide',
      items: [
        { text: 'Install', link: '/install' },
        { text: 'Quickstart', link: '/quickstart' },
        { text: 'Configuration', link: '/configuration' },
        { text: 'Commands', link: '/commands' }
      ]
    },
    { text: 'How it works', link: '/how-it-works' }
  ],

  sidebar: [
    {
      text: 'Guide',
      items: [
        { text: 'Install', link: '/install' },
        { text: 'Quickstart', link: '/quickstart' },
        { text: 'Configuration', link: '/configuration' },
        { text: 'Commands', link: '/commands' }
      ]
    },
    {
      text: 'Reference',
      items: [
        { text: 'How it works', link: '/how-it-works' },
        { text: 'Agent (launchd)', link: '/agent-launchd' },
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
})
