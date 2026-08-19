# python-OBD Architecture

The project pins `obd==0.7.3`.

`obd.Async` owns the live OBD connection. Supported live commands are discovered from `connection.supported_commands`. Core signals are watched automatically when supported. Optional discovered Mode 01 commands can be added/removed at runtime from the web app with `watch()`/`unwatch_all()` reconfiguration.

Static/slow queries such as VIN and DTCs are queried separately while the async loop is paused.
