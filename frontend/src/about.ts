// The Guide tab: how FireLens works, where its data comes from and how to read it.
import { getLang } from "./prefs";

const EN = `
  <div class="guide">
    <h2>How FireLens works</h2>
    <p class="lede">FireLens watches the planet for fire from space. It collects every fire NASA's satellites detect,
      groups the detections into fires, and adds context from European satellites, land maps and weather, so you can
      see where fires are, how they evolve and whether this season is unusual.</p>

    <ol class="guide-steps">
      <li><span><b>Look at the map.</b> Each mark is fire seen from space. Zoomed out, nearby detections are grouped in numbered bubbles; zoom in to see each fire.</span></li>
      <li><span><b>Choose the period</b> in Filters: the latest 24 h, 48 h or 7 days, or any past dates in History.</span></li>
      <li><span><b>Click a fire</b> for its story: how big and strong it is, what it burns, and a satellite report (burn scar, smoke, heat).</span></li>
      <li><span><b>Analyse an area:</b> point the globe at a region and press <b>Analyse</b> in the Analysis tab to see trends, past seasons and what is burning.</span></li>
      <li><span><b>Stay informed:</b> create alert rules for the places you care about in the Alerts tab.</span></li>
    </ol>

    <div class="note"><b>Simple</b> shows the essentials as graphics. <b>Advanced</b> shows every number, filter and method detail. Switch at the top of the panel; the choice is remembered.</div>

    <details open>
      <summary>Where the data comes from</summary>
      <div>
        <table class="sources">
          <tr><td>NASA FIRMS</td><td>Active-fire detections from <b>MODIS</b> (Terra, Aqua; 1 km pixels) and <b>VIIRS</b> (Suomi NPP, NOAA-20, NOAA-21; 375 m pixels). Near real time, worldwide, plus the science-quality archive.</td></tr>
          <tr><td>Copernicus Sentinel-2</td><td>Vegetation greenness and moisture before a fire, and the burn scar after it (10–20 m).</td></tr>
          <tr><td>Copernicus Sentinel-3</td><td>Daytime ground temperature (1 km).</td></tr>
          <tr><td>Copernicus Sentinel-5P</td><td>Smoke: carbon monoxide and the UV aerosol index.</td></tr>
          <tr><td>ESA WorldCover</td><td>Land cover: forest, shrubland, grassland, cropland, towns, water (2021, 10 m).</td></tr>
          <tr><td>Copernicus DEM</td><td>Elevation and slope (90 m).</td></tr>
          <tr><td>Open-Meteo, Meteomatics</td><td>Weather forecasts and history: wind, humidity, temperature, rain.</td></tr>
        </table>
      </div>
    </details>

    <details>
      <summary>From detections to fires</summary>
      <div>
        <p>A <b>detection</b> is one satellite pixel that saw unusual heat on one pass. A real fire usually produces many detections: several pixels, several passes, several satellites.</p>
        <p>FireLens groups detections that touch in space and follow each other in time into a <b>fire event</b>. Each fire keeps a history: when it started, how it grew, whether it merged with another one, and its fire power on every satellite pass.</p>
        <p>Gas flares, volcanoes and offshore platforms are recognised and kept out of fires and alerts.</p>
      </div>
    </details>

    <details>
      <summary>How fresh the data is</summary>
      <div>
        <p>NASA publishes each satellite pass about 3 hours after it flies over. FireLens checks for new data every hour and the map refreshes itself every 5 minutes.</p>
        <p>Each place is seen by the satellites only a few times a day, and clouds or thick smoke can hide a fire. <b>No recent detection does not mean a fire is out.</b></p>
      </div>
    </details>

    <details>
      <summary>What the analyses mean</summary>
      <div>
        <p><b>Statistics:</b> detections and fires in the area and period, day by day, compared with the previous period of the same length.</p>
        <p><b>Compared with past seasons:</b> the same calendar days of earlier years, counted with the same satellites over the same area. "Above normal" only when it is outside the range of every past season.</p>
        <p><b>What is burning:</b> the land cover under each detection (forest, grassland, cropland…). Cropland fires are mostly agricultural burning.</p>
        <p><b>Satellite report of a fire:</b> burned area and severity from Sentinel-2, vegetation before the fire, ground heat and smoke.</p>
        <p><b>Fire-danger forecast:</b> a 7-day index from the weather forecast, recent fire activity and vegetation.</p>
        <p><b>Spread outlook (experimental):</b> where a burning fire could go in 24 h from land cover, terrain and wind. In tests on past fires it did not beat a simple circle around the fire, so it is labelled "not validated".</p>
      </div>
    </details>

    <details>
      <summary>Limits to keep in mind</summary>
      <div>
        <p>Counts measure fire activity seen from space, not the number of fires or the area burned. Small or short fires, fires under clouds and fires between passes can be missed.</p>
        <p>Fire power (FRP) is the heat at the moment of a pass; MODIS and VIIRS are not cross-calibrated, so their values are shown separately.</p>
        <p>Land cover is a 2021 map: land cleared since then appears with its 2021 class.</p>
      </div>
    </details>

    <div class="note">FireLens is an information tool. <b>In an emergency, follow the instructions of your local authorities.</b></div>
  </div>`;

const ES = `
  <div class="guide">
    <h2>Cómo funciona FireLens</h2>
    <p class="lede">FireLens vigila el fuego en el planeta desde el espacio. Reúne cada foco que detectan los satélites
      de la NASA, agrupa las detecciones en incendios y suma contexto de satélites europeos, mapas del terreno y clima,
      para que veas dónde hay fuego, cómo evoluciona y si esta temporada es inusual.</p>

    <ol class="guide-steps">
      <li><span><b>Mirá el mapa.</b> Cada marca es fuego visto desde el espacio. Con el mapa alejado, las detecciones cercanas se agrupan en burbujas numeradas; acercate para ver cada foco.</span></li>
      <li><span><b>Elegí el período</b> en Filtros: las últimas 24 h, 48 h o 7 días, o cualquier fecha pasada en Historial.</span></li>
      <li><span><b>Tocá un incendio</b> para conocer su historia: qué tan grande y fuerte es, qué está quemando y un informe satelital (cicatriz, humo, calor).</span></li>
      <li><span><b>Analizá una zona:</b> apuntá el globo a una región y tocá <b>Analizar</b> en la pestaña Análisis para ver tendencias, temporadas pasadas y qué se quema.</span></li>
      <li><span><b>Mantenete informado:</b> creá reglas de alerta para los lugares que te importan en la pestaña Alertas.</span></li>
    </ol>

    <div class="note"><b>Simple</b> muestra lo esencial en gráficos. <b>Avanzada</b> muestra cada número, filtro y detalle del método. Cambiá arriba del panel; la elección se recuerda.</div>

    <details open>
      <summary>De dónde salen los datos</summary>
      <div>
        <table class="sources">
          <tr><td>NASA FIRMS</td><td>Detecciones de fuego activo de <b>MODIS</b> (Terra, Aqua; píxeles de 1 km) y <b>VIIRS</b> (Suomi NPP, NOAA-20, NOAA-21; píxeles de 375 m). Casi en tiempo real, en todo el mundo, más el archivo de calidad científica.</td></tr>
          <tr><td>Copernicus Sentinel-2</td><td>Verdor y humedad de la vegetación antes del incendio, y la cicatriz de quema después (10–20 m).</td></tr>
          <tr><td>Copernicus Sentinel-3</td><td>Temperatura del suelo durante el día (1 km).</td></tr>
          <tr><td>Copernicus Sentinel-5P</td><td>Humo: monóxido de carbono e índice de aerosoles UV.</td></tr>
          <tr><td>ESA WorldCover</td><td>Cobertura del suelo: bosque, arbustal, pastizal, cultivos, ciudades, agua (2021, 10 m).</td></tr>
          <tr><td>Copernicus DEM</td><td>Elevación y pendiente (90 m).</td></tr>
          <tr><td>Open-Meteo, Meteomatics</td><td>Pronósticos e historial del clima: viento, humedad, temperatura, lluvia.</td></tr>
        </table>
      </div>
    </details>

    <details>
      <summary>De detecciones a incendios</summary>
      <div>
        <p>Una <b>detección</b> es un píxel satelital que vio un calor anormal en una pasada. Un incendio real suele dar muchas detecciones: varios píxeles, varias pasadas, varios satélites.</p>
        <p>FireLens agrupa las detecciones que se tocan en el espacio y se suceden en el tiempo en un <b>incendio</b>. Cada incendio guarda su historia: cuándo empezó, cómo creció, si se unió con otro y su potencia en cada pasada satelital.</p>
        <p>Las antorchas de gas, los volcanes y las plataformas marinas se reconocen y quedan fuera de los incendios y las alertas.</p>
      </div>
    </details>

    <details>
      <summary>Qué tan recientes son los datos</summary>
      <div>
        <p>La NASA publica cada pasada unas 3 horas después del sobrevuelo. FireLens busca datos nuevos cada hora y el mapa se actualiza solo cada 5 minutos.</p>
        <p>Cada lugar es visto por los satélites solo algunas veces por día, y las nubes o el humo espeso pueden ocultar un fuego. <b>Sin detección reciente no significa que el fuego esté apagado.</b></p>
      </div>
    </details>

    <details>
      <summary>Qué significan los análisis</summary>
      <div>
        <p><b>Estadísticas:</b> detecciones e incendios de la zona y el período, día por día, comparados con el período anterior de igual duración.</p>
        <p><b>Comparado con temporadas pasadas:</b> los mismos días del calendario de años anteriores, contados con los mismos satélites sobre la misma zona. "Por encima de lo normal" solo cuando supera el rango de todas las temporadas pasadas.</p>
        <p><b>Qué se quema:</b> la cobertura del suelo bajo cada detección (bosque, pastizal, cultivos…). Los fuegos en cultivos son en su mayoría quemas agrícolas.</p>
        <p><b>Informe satelital de un incendio:</b> área quemada y severidad según Sentinel-2, vegetación antes del fuego, calor del suelo y humo.</p>
        <p><b>Pronóstico de peligro de incendio:</b> un índice a 7 días según el pronóstico del clima, la actividad reciente y la vegetación.</p>
        <p><b>Propagación (experimental):</b> hacia dónde podría avanzar un incendio activo en 24 h según el terreno, la vegetación y el viento. En pruebas con incendios pasados no superó a un simple círculo alrededor del fuego, por eso figura como "no validado".</p>
      </div>
    </details>

    <details>
      <summary>Límites a tener en cuenta</summary>
      <div>
        <p>Los conteos miden la actividad de fuego vista desde el espacio, no la cantidad de incendios ni el área quemada. Pueden escaparse fuegos chicos o breves, fuegos bajo las nubes y fuegos entre pasadas.</p>
        <p>La potencia (FRP) es el calor en el momento de una pasada; MODIS y VIIRS no están intercalibrados, por eso sus valores se muestran por separado.</p>
        <p>La cobertura del suelo es un mapa de 2021: lo desmontado después aparece con su clase de 2021.</p>
      </div>
    </details>

    <div class="note">FireLens es una herramienta de información. <b>Ante una emergencia, seguí las indicaciones de las autoridades locales.</b></div>
  </div>`;

/** Fill the Guide tab in the current language. */
export function mountGuideTab(el: HTMLElement): void {
  el.innerHTML = getLang() === "es" ? ES : EN;
}
