/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.js'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#090A0F', // deep obsidian: page
          900: '#0F1117', // slate dark: sections, windows
          800: '#161922', // surface elevate: used at 60% under blur
        },
        electric: '#00F2FE',
        neon: '#7F00FF',
        pulse: '#10B981',
      },
      fontFamily: {
        sans: ['"Plus Jakarta Sans Variable"', '"Plus Jakarta Sans"', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono Variable"', '"JetBrains Mono"', '"Fira Code"', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        // Display scale: 1.25 ratio from a 16px base, tight leading for headlines.
        'display-sm': ['2.5rem', { lineHeight: '1.05', letterSpacing: '-0.035em' }],
        'display-md': ['3.75rem', { lineHeight: '1.02', letterSpacing: '-0.04em' }],
        'display-lg': ['5rem', { lineHeight: '0.98', letterSpacing: '-0.045em' }],
      },
      boxShadow: {
        lift: '0 1px 0 0 rgba(255,255,255,0.04) inset, 0 20px 40px -24px rgba(0,0,0,0.8)',
        glow: '0 0 0 1px rgba(0,242,254,0.35), 0 8px 32px -6px rgba(0,242,254,0.45), 0 2px 18px -4px rgba(127,0,255,0.5)',
      },
      keyframes: {
        aura: {
          '0%,100%': { opacity: '0.55', transform: 'scale(0.92)' },
          '50%': { opacity: '1', transform: 'scale(1.12)' },
        },
        blink: { '0%,100%': { opacity: '1' }, '50%': { opacity: '0' } },
        ping: { '75%,100%': { transform: 'scale(2.2)', opacity: '0' } },
      },
      animation: {
        aura: 'aura 3.2s ease-in-out infinite',
        blink: 'blink 1.05s steps(1) infinite',
        ping: 'ping 1.6s cubic-bezier(0,0,0.2,1) infinite',
      },
    },
  },
  plugins: [],
};
