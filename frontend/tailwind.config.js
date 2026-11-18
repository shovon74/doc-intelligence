/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: "#1A365D",
        amber: "#D97706",
        cream: "#FDF9F3",
        panel: "#F1F4F8",
        ink: "#1A1C21",
        muted: "#4B5563",
      },
    },
  },
  plugins: [],
};
