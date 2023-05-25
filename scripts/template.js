nav_link = document.querySelectorAll("a");

nav_link.forEach(element => element.addEventListener("mouseover", event => {
    element.style.color = "white";
}));

nav_link.forEach(element => element.addEventListener("mouseout", event => {
    element.style.color = "gray";
}));