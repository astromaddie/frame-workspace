"use strict";

// --config replaces package.json's build object. Preserve every inherited
// Frame Control option and add only its custom install-link protocol.
const build = JSON.parse(JSON.stringify(require("../package.json").build));

module.exports = {
  ...build,
  protocols: [
    {
      name: "Frame Control install link",
      schemes: ["frame-control"],
    },
  ],
};
