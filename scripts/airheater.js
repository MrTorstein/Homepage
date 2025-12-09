var footer = document.querySelector("footer")

window.addEventListener('scroll', () => {
    if (window.scrollY > 800) {
        footer.style.display = "block";
    }
    else {
        footer.style.display = "none"
    }
});

nav_link = document.querySelectorAll("footer a");

nav_link.forEach(element => element.addEventListener("mouseout", event => {
    element.style.color = "var(--Day-9-yellow)";
}));