# Mis tareas (Columbia + UNISAL)

Junta las tareas pendientes de tus dos aulas virtuales en una sola página para el
teléfono, y se actualiza sola cada 3 horas con GitHub Actions (aunque tu PC esté apagado).

- **Columbia** (Moodle): entra como la app Moodle y trae solo lo pendiente,
  igual que el bloque "Línea de tiempo".
- **UNISAL** (Chamilo): entra a la plataforma, lee "Mi agenda" y descarta las
  tareas que ya enviaste. También muestra exámenes y eventos de tus cursos.

Archivos:
- `actualizar.py`: lee las dos plataformas y genera `docs/tareas.json`.
- `docs/index.html`: la página (vencidas, hoy, mañana, 7 días, etc.). Tocas una
  tarea para ver el detalle y el cuadrito para marcarla como hecha.
- `.github/workflows/actualizar.yml`: la corre cada 3 horas y la publica en GitHub Pages.

## 1. Pon tus datos

Abre `config.json` y reemplaza `TU_USUARIO_…` y `TU_CONTRASEÑA_…` con tus datos
de cada plataforma. `config.json` está en `.gitignore`: nunca se sube a GitHub.

En UNISAL, la lista `"cursos"` dice qué materias mirar (sin importar tildes ni
mayúsculas; basta una parte del nombre). Así se ignoran las materias de años
anteriores. **Cuando empiece un semestre nuevo, cambia esa lista** (y el secreto
`TAREAS_CONFIG` en GitHub). Si la quitas, se revisan todos tus cursos.

## 2. Pruébalo en tu PC

```powershell
python actualizar.py
```

```powershell
python -m http.server 8000 --directory docs
```

Luego abre http://localhost:8000.

## 3. Publícalo para que se actualice solo

1. Sube esta carpeta a un repositorio de GitHub.
2. En el repositorio: **Settings → Secrets and variables → Actions → New repository secret**,
   nombre `TAREAS_CONFIG` y como valor pega **todo el contenido de tu `config.json`**.
   GitHub lo guarda cifrado: nadie puede verlo, solo lo usa el workflow.
3. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions → Actualizar tareas → Run workflow** para la primera actualización.
5. Abre `https://<tu-usuario>.github.io/<repositorio>/` en el teléfono y, en el menú
   del navegador, **Agregar a pantalla de inicio**.

Las tareas no se guardan en el repositorio: solo se publican en la página.
Ojo: con GitHub gratis la página es pública para quien tenga el enlace (no aparece en
buscadores, pero se puede abrir). Tus usuarios y contraseñas nunca aparecen en ella.

## Si algo falla

- Si una plataforma no responde, la página muestra un aviso naranja y mantiene sus
  tareas de la última vez que funcionó.
- **Si cambias tu contraseña** en alguna plataforma, actualízala en `config.json` y en
  el secreto `TAREAS_CONFIG`.
- GitHub puede atrasar las ejecuciones programadas unos minutos.
- En UNISAL, una tarea aparece solo si el profesor la puso con fecha en la agenda
  (es lo normal: Chamilo lo hace al crear la tarea con fecha límite).
