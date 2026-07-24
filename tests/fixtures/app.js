// Mimics a compiled Svelte/React bundle: the served HTML contains no links
// at all, the DOM is built at runtime.
var routes = [
  ["/about.html", "JS About"],
  ["/team.html", "JS Team"],
  ["/js-gone.html", "JS dead link"],
  ["https://example.com", "JS external"]
];
var app = document.getElementById("app");
routes.forEach(function (route) {
  var a = document.createElement("a");
  a.setAttribute("href", route[0]);
  a.textContent = route[1];
  app.appendChild(a);
});
