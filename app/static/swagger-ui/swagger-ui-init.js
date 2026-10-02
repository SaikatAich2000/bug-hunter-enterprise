window.addEventListener("DOMContentLoaded", function () {
  window.ui = SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    displayOperationId: true,
    filter: true,
    presets: [
      SwaggerUIBundle.presets.apis,
    ],
    layout: "BaseLayout",
  });
});