/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: [
    "./src/poghiamo/webapp/templates/**/*.html",
  ],
  theme: {
    extend: {
      colors: {
        primary: "#f43f5e",
        background: "#0e0e11",
        card: "#17171c",
        cream: "#f2f0e9",
      },
      fontFamily: {
        sans: ["system-ui", "-apple-system", "sans-serif"],
      },
    },
  },
  plugins: [require("@tailwindcss/forms")],
};
