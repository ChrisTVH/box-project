# Spanish glossary

Shared source of truth for both projects. The first section covers the backend (`box-rpg`); the second covers frontend-only concepts (`box-gui`). Terms from the first section apply to the frontend too and are not repeated. Notes are in Spanish and state the context plus what stays in English.

## Backend (`box-rpg`)

| English | Spanish | Notes |
| --- | --- | --- |
| game root | raíz del juego | Mantener los selectores de comando sin cambios. |
| runtime | entorno de ejecución | Mantener el comando `runtime` sin cambios. |
| managed data | datos gestionados | |
| download archive | archivo de descarga | |
| game profile | perfil del juego | |
| isolated session | sesión aislada | |
| cache | caché | |
| configuration | configuración | |
| cleanup | limpieza | Mantener el comando `cleanup` sin cambios. |
| symlink | enlace simbólico | |
| cleanup category | categoría de limpieza | |
| cleanup selector | selector de limpieza | |
| game manifest | manifiesto del juego | |
| entrypoint | punto de entrada | |
| launcher | iniciador | |
| launch session | sesión de inicio | |
| manifest | manifiesto | |
| plugin | complemento | |
| version index | índice de versiones | |
| download path | ruta de descarga | |
| managed directory | directorio gestionado | |
| allowed game root | raíz de juego permitida | El mensaje de autorización lo muestra como «juegos permitidos». |
| authorized game root | raíz de juego autorizada | |
| game-root file | archivo de la raíz del juego | |
| host | servidor | En mensajes técnicos. |
| ownership | propiedad | |
| web game | juego web | |
| completion | autocompletado | En mensajes del autocompletado del shell. |
| download | descarga | |
| EasyRPG download | descarga de EasyRPG | |
| session | sesión | |
| EasyRPG runtime | entorno de ejecución de EasyRPG | |
| confirmed game root | raíz del juego confirmada | |
| byte limit | límite de bytes | |
| game file | archivo del juego | |
| traversal | recorrido | En rutas: «sin recorrido». |
| regular file | archivo regular | Término de Unix. |
| Bubblewrap | Bubblewrap | Nombre de herramienta; no traducir. |
| GnuPG | GnuPG | Nombre de herramienta; no traducir. |
| sandbox | entorno aislado | En «game sandbox». |
| user namespaces | espacios de nombres de usuario | Función del núcleo Linux. |
| install plan | plan de instalación | |
| private directory | directorio privado | |
| version list | lista de versiones | |
| version page | página de versiones | |
| game directory | directorio del juego | En textos de ayuda; distinto de «managed directory» → «directorio gestionado». |
| current directory | directorio actual | En «(predeterminado: directorio actual)». |
| runtime version | versión del entorno de ejecución | |
| configuration key | clave de configuración | Las claves (allowed-game-root, preferred-runtime) y `none` quedan en inglés. |
| GUI | GUI | Femenino: «la GUI», «GUI instalada». No traducir la sigla. |
| CLI | CLI | Femenino: «la CLI». No traducir la sigla. |
| distribution | distribución | En «select which distributions to manage». |
| managed file | archivo gestionado | Plural masculino «archivos gestionados»; paralelo a «managed directory». |
| JSON Lines | JSON Lines | Formato de un objeto JSON por línea; claves y selectores quedan en inglés. |
| GameMode | GameMode | Nombre de producto; no traducir. Mantener `gamemoderun`, la ruta `/usr/bin/gamemoderun` y la opción `--gamemode` sin cambios. |
| supervisor status | estado del supervisor | Estado escrito por el supervisor en `status.json`; usado en `stop_session`. |
| launch status | estado de inicio | Estado registrado en `status.json`; distinto de «launch session» → «sesión de inicio». |
| name collision | colisión de nombres | En «cannot create launch session: name collision». |
| session bus | bus de sesión | En «session bus address»; las variables `$DBUS_SESSION_BUS_ADDRESS` y `$XDG_RUNTIME_DIR/bus` quedan literales. |
| packed executable | ejecutable empaquetado | En mensajes de EVB; `Game.exe`, `evbunpack` y `Enigma Virtual Box` quedan literales. |
| unpacked game | juego desempaquetado | Etiqueta de caché en `paths.py`; también en «unpacked game cache name». |
| packed source | origen empaquetado | Origen del ejecutable en `evb.py`; «source» → «origen». |
| packed file | archivo empaquetado | En «per-file size budget». |
| budget | presupuesto | En «source size budget», «entry count budget», «time budget», etc. Distinto de «byte limit» → «límite de bytes». |
| output directory | directorio de salida | En mensajes de `evb_unpack.py`. |
| extraction root | raíz de extracción | En «refusing path outside extraction root». |
| loader | cargador | En «loader sections» y «loader data». |
| preserved image | imagen preservada | Imagen preservada en mensajes de importación y reubicación. |
| import directory | directorio de importación | Directorio PE recuperado. |
| relocation directory | directorio de reubicación | Directorio PE recuperado. |
| exception record | registro de excepciones | En mensajes de restauración; «exception handling» → «control de excepciones». |
| thread directory | directorio de hilos | En «thread directory cleared»; «thread» → «hilo». |

## Frontend (`box-gui`)

Terms taken from the `box-rpg-maker` catalog; if a msgid contains a backend term, the backend Spanish above wins (adjust agreement as needed).

| English | Spanish | Notes |
| --- | --- | --- |
| Library | Biblioteca | Ventana principal de la app. |
| Add game | Añadir juego | Botón y acción de la biblioteca. |
| Game Detail | Detalle del juego | Página de detalle del juego. |
| Display name | Nombre visible | Fila editable; el archivo guarda `display_name` en inglés. |
| Icon | Icono | Fila de icono del juego. |
| executable | ejecutable | En el selector de ejecutable del juego. |
| Engine default | Icono del motor | Texto del catálogo; revisar si cambia la fuente. |
| Change… | Cambiar… | Botón de cambio de icono; mantener los puntos suspensivos. |
| Settings | Ajustes | Diálogo de ajustes. |
| Allowed game roots | Raíces de juego permitidas | Página General; plural de «allowed game root». |
| Add root | Añadir raíz | Acción de autorización; `root` aquí es forma corta de «game root». |
| Preferred runtime | Entorno de ejecución preferido | Fila de la página de detalle. |
| Preferred NW.js runtime | Entorno NW.js preferido | Página de ajustes; `NW.js` no se traduce. |
| Preferred EasyRPG runtime | Entorno EasyRPG preferido | Página de ajustes; `EasyRPG` no se traduce. |
| Sandbox permissions | Permisos de la caja | El catálogo usa «caja»; mantenerlo por coherencia. |
| Allow network usage | Permitir uso de red | Interruptor de la página de detalle. |
| Allow modifying the game | Permitir modificar en el juego | Interruptor de la página de detalle; para juegos con actualización automática. |
| Allow X11 or XWayland sessions | Permitir sesiones de X11 o XWayland | `X11` y `XWayland` no se traducen. |
| Diagnose | Diagnosticar | Botón y diálogo de diagnóstico. |
| Additional files | Archivos adicionales | Grupo de la página de detalle. |
| Environment | Entorno | Grupo del diálogo de diagnóstico. |
| Versions | Versiones | Grupo del diálogo de diagnóstico. |
| Data | Datos | Página de ajustes; el nombre interno queda en inglés. |
| Game profiles | Perfiles de juego | Categoría de limpieza de la GUI. |
| Open Runtimes | Abrir entornos | Respuesta sugerida del diálogo de error. |
| Add root | Añadir raíz | Acción de autorización; `root` aquí es forma corta de «game root». |
