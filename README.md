# Canalización de Coordenadas → Códigos Postales en AWS

> **Nota:** En el momento de redactar esto, la función de ingestión Lambda falla al enviar mensajes a SQS debido a un problema de permisos de IAM. Ver **Problemas Conocidos** al final.

---

## Tabla de Contenidos

1. [Descripción General](#descripción-general)
2. [Diagrama de Arquitectura](#diagrama-de-arquitectura)
3. [Componentes y Flujo](#componentes-y-flujo)
4. [Estructura de Archivos](#estructura-de-archivos)
5. [Pasos de Despliegue y Uso](#pasos-de-despliegue-y-uso)
6. [Problemas Conocidos y Recomendaciones](#problemas-conocidos-y-recomendaciones)
7. [Pruebas y Depuración](#pruebas-y-depuración)
8. [Flujo de Trabajo con Git](#flujo-de-trabajo-con-git)

---

## Descripción General

Este proyecto implementa una canalización sin servidor en AWS en dos fases para procesar un archivo CSV grande (`coordenates.csv`, \~80 MB, \~1 M filas) de pares de latitud/longitud y enriquecer cada fila con el código postal más cercano mediante la API pública de [`postcodes.io`](https://postcodes.io/).

1. **Fase de Carga**

   * Un usuario solicita una URL pre-firmada de S3 (mediante un endpoint API “Presign”) y luego carga el CSV bruto en S3.

2. **Fase de Ingestión**

   * Una Lambda desencadenada por S3 (`IngestFunction`) lee el CSV recién cargado, lo analiza por lotes y encola cada par de coordenadas (o un pequeño lote) en una cola SQS (`CoordinatesQueue`).

3. **Fase de Procesamiento**

   * Otra Lambda (`ProcessFunction`) está suscrita a `CoordinatesQueue`. Para cada mensaje (par de coordenadas), llama a `postcodes.io` para obtener los detalles del código postal más cercano y escribe el registro enriquecido en DynamoDB (`CoordinatesTable`).

4. **Almacenamiento de Datos**

   * DynamoDB almacena cada coordenada original junto con los detalles de su código postal asociado.

Visualmente, el flujo es:
```mermaid
flowchart LR
  subgraph API Layer
    PresignAPI["PresignFunction (Lambda)"]
    APIGW["API Gateway"]
  end

  subgraph Storage Layer
    S3Bucket["CoordinatesBucket (S3)"]
  end

  subgraph Ingestion Layer
    IngestFunction["IngestFunction (Lambda)"]
    CoordinatesQueue["CoordinatesQueue (SQS)"]
  end

  subgraph Processing Layer
    ProcessFunction["ProcessFunction (Lambda)"]
    DLQ["CoordinatesDLQ (SQS DLQ)"]
  end

  subgraph Data Layer
    DynamoDB["CoordinatesTable (DynamoDB)"]
  end

  user[Usuario] -->|POST /presign { key: "coordenates.csv" }| APIGW
  APIGW --> PresignAPI
  PresignAPI -->|retorna URL PUT pre-firmada| user
  user -->|PUT CSV bruto| S3Bucket
  S3Bucket -->|s3:ObjectCreated:*| IngestFunction
  IngestFunction -->|SendMessage| CoordinatesQueue
  CoordinatesQueue -->|Invoca| ProcessFunction
  ProcessFunction -->|Inserción por lotes| DynamoDB
  ProcessFunction -->|en caso de fallo| DLQ
```
---

## Componentes y Flujo

1. **`CoordinatesBucket` (S3)**

   * Almacena las cargas de CSV sin procesar.
   * El nombre del bucket es `<stack-name>-coordinates-bucket` (todo en minúsculas).
   * No hay `NotificationConfiguration` manual en la plantilla; SAM genera automáticamente el trigger S3→Lambda.

2. **`PresignFunction` (Lambda) + API Gateway**

   * **Endpoint:** `POST /presign`
   * **Propósito:** Genera una URL PUT pre-firmada de S3, para que los clientes carguen archivos grandes directamente a S3 sin necesitar credenciales de AWS en el cliente.
   * **Política IAM:** `S3WritePolicy` con alcance a `CoordinatesBucket`.

3. **`IngestFunction` (Lambda)**

   * **Desencadenador:** `s3:ObjectCreated:*` en `CoordinatesBucket` (cuando se carga un nuevo CSV).
   * **Tarea:**

     1. Descargar el CSV bruto desde S3.
     2. Analizarlo línea por línea (saltando/validando encabezado).
     3. Para cada fila (o pequeño lote), formar un mensaje JSON con `{ id, latitude, longitude }` y llamar a `SQS.sendMessage(...)` para encolarlo en `CoordinatesQueue`.
     4. Manejar errores de validación del CSV (por ejemplo, formato inválido) e informarlos a través de CloudWatch Logs o en una tabla “errores” separada (SQS o DynamoDB) si se requiere.
   * **Políticas IAM Requeridas:**

     * `S3ReadPolicy` en `CoordinatesBucket`.
     * `SQSSendMessagePolicy` en `CoordinatesQueue`.
     * `DynamoDBCrudPolicy` en `CoordinatesTable` (si se necesitaran búsquedas inmediatas o escrituras; en el diseño actual no se escribe aquí).

4. **`CoordinatesQueue` (SQS)**

   * **Propósito:** Buffer de mensajes de coordenadas entrantes desde `IngestFunction` para desacoplar la ingestión del procesamiento.
   * **Propiedades:**

     * `VisibilityTimeout = 900` segundos (15 minutos), coincide con el timeout de Lambda, para que un mensaje no se reprocesse prematuramente.
   * **Cola de Mensajes Fallidos (`CoordinatesDLQ`):**

     * Almacena mensajes fallidos tras el conteo máximo de recepciones, para que puedan inspeccionarse manualmente.

5. **`ProcessFunction` (Lambda)**

   * **Desencadenador:** Mapeo de origen de eventos en `CoordinatesQueue`.
   * **BatchSize:** 10 (lee hasta 10 mensajes a la vez).
   * **Tarea:** Para cada mensaje de coordenadas:

     1. Llamar a `https://api.postcodes.io/postcodes?lon={longitude}&lat={latitude}` (o al endpoint `/nearest`) para encontrar el código postal más cercano.
     2. Si tiene éxito, armar un registro completo:

        ```json
        {
          "id": "<uuid-o-índice-de-fila>",
          "latitude": <float>,
          "longitude": <float>,
          "postcode": "<por ejemplo SW1A 1AA>",
          "admin_district": "...",
          "region": "...",
          "parliamentary_constituency": "...",
          // etc. (otros campos que devuelva la API)
        }
        ```
     3. Escribir (upsert) el registro en la tabla DynamoDB `CoordinatesTable`, con clave `id`.
     4. Si la búsqueda del código postal falla para una coordenada (por ejemplo, no se encuentra código postal o se excede el límite de solicitudes), registrar el error y enviar ese mensaje a la DLQ tras reintentos.
   * **Política IAM:**

     * `DynamoDBCrudPolicy` en `CoordinatesTable` para `PutItem`.
     * (Opcional) No se requiere permiso IAM para llamar al endpoint externo `postcodes.io`.

6. **`CoordinatesTable` (DynamoDB)**

   * **Esquema:**

     * **Clave Primaria:** `id` (cadena; UUID o índice de fila)
     * **Atributos:**

       * `latitude` (Número)
       * `longitude` (Número)
       * `postcode` (Cadena)
       * Atributos adicionales relacionados con el código postal (región, distrito, etc.)
   * **Modo de Facturación:** `PAY_PER_REQUEST` (On-Demand) para escalado automático y eficiente en costos.

---

## Estructura de Archivos

```
├── README.md
├── template.yaml                ← Plantilla AWS SAM
├── presign/                     ← Código para PresignFunction
│   ├── app.py
│   ├── requirements.txt
│   └── Dockerfile (si se construye en contenedor)
├── ingest/                      ← Código para IngestFunction
│   ├── app.py
│   ├── requirements.txt
│   └── Dockerfile (si se construye en contenedor)
├── process/                     ← Código para ProcessFunction
│   ├── app.py
│   ├── requirements.txt
│   └── Dockerfile (si se construye en contenedor)
└── samconfig.toml               ← Configuración (auto) de SAM CLI
```

* **`template.yaml`** define todos los recursos de AWS (S3, SQS, DynamoDB, Lambdas, API Gateway).
* Cada carpeta de Lambda (`presign/`, `ingest/`, `process/`) contiene:

  * `app.py` (handler de la función)
  * `requirements.txt` (dependencias)

---

## Pasos de Despliegue y Uso

### 1. Requisitos Previos

* **AWS CLI** configurada con un perfil/credenciales que tenga permisos para crear roles IAM, funciones Lambda, buckets S3, colas SQS, tablas DynamoDB y recursos de API Gateway.

* **AWS SAM CLI** instalado (v1.XX o superior).

* **Python 3.10** en tu PATH local (SAM lo utiliza para compilar dependencias).

  ```bash
  # En Linux/Mac:
  python3.10 --version
  # En Windows (PowerShell):
  py -3.10 --version
  ```

* (Opcional) **Docker** instalado, si planeas compilar con `--use-container`.

### 2. Clonar e Inspeccionar

```bash
git clone <tu-repo-url> coordinates-pipeline
cd coordinates-pipeline
```

Revisa `template.yaml`—define:

* **Bucket S3** `CoordinatesBucket`
* **Tabla DynamoDB** `CoordinatesTable`
* **Cola SQS** `CoordinatesQueue` + `CoordinatesDLQ`
* **Funciones Lambda**: `PresignFunction`, `IngestFunction`, `ProcessFunction`
* **API Gateway** para el endpoint `/presign`

### 3. Construir las Lambdas

Desde la raíz del proyecto, ejecuta:

```bash
sam build
```

> Si te encuentras con un error “Python 3.10 not found”, asegúrate de que tu Python 3.10 esté en el `PATH`, o usa Docker:

```bash
sam build --use-container
```

### 4. Desplegar la Pila

```bash
sam deploy --guided
```

Se te pedirá:

* **Stack Name** (por ejemplo, `mopr3`)
* **AWS Region** (por ejemplo, `us-east-1`)
* **¿Permitir que SAM CLI cree roles IAM?** → `Y`
* **¿Deshabilitar rollback?** → `N` (para que los despliegues fallidos se retrotraigan automáticamente)
* **¿Guardar argumentos en un archivo de configuración?** → `Y`

Una vez confirmado, SAM sube la plantilla empaquetada a S3 y crea:

* **Bucket S3** (por ejemplo, `mopr3-coordinates-bucket`)
* **Tabla DynamoDB** (`coordinates`)
* **Cola SQS** (`mopr3-CoordinatesQueue-XXXX`)
* **DLQ** (`mopr3-CoordinatesDLQ-XXXX`)
* **Roles de Lambda** y **Lambdas**
* **Endpoint de API Gateway** para `/presign`

Al final, SAM imprime el `ApiUrl`. Por ejemplo:

```
ApiUrl: https://abcdefg123.execute-api.us-east-1.amazonaws.com/Prod
```

### 5. Cargar el CSV vía URL Pre-firmada

1. **Solicitar una URL pre-firmada:**

   ```bash
   export API_URL="https://abcdefg123.execute-api.us-east-1.amazonaws.com/Prod"
   presign=$(curl -s -X POST "$API_URL/presign" \
     -H "Content-Type: application/json" \
     -d '{"key":"coordenates.csv"}')
   uploadUrl=$(echo "$presign" | jq -r .url)
   echo "Pre-signed PUT URL: $uploadUrl"
   ```

2. **Cargar el `coordenates.csv` local:**

   ```bash
   curl -v -X PUT "$uploadUrl" \
     -H "Content-Type: text/csv" \
     --upload-file "./coordenates.csv"
   ```

   Si tiene éxito, S3 devuelve `200 OK`. En ese momento, S3 genera un evento `ObjectCreated:Put`, que invoca `IngestFunction`.

### 6. (Previsto) Ingestión → SQS → Procesamiento → DynamoDB

* **`IngestFunction`** debería leer el CSV desde S3, analizar cada fila y llamar a `SQS.sendMessage(...)` para encolar mensajes.
* **`CoordinatesQueue`** recibirá \~1 M mensajes (uno por coordenada).
* **`ProcessFunction`** recoge mensajes (en lotes de 10), llama a la API externa `postcodes.io` para cada coordenada y escribe el registro enriquecido en `CoordinatesTable`.

Puedes monitorear el progreso mediante:

1. **Número aproximado de mensajes en SQS**

   ```bash
   aws sqs get-queue-attributes \
     --queue-url https://sqs.us-east-1.amazonaws.com/<acct-id>/<CoordinatesQueueName> \
     --attribute-names ApproximateNumberOfMessages \
     --region us-east-1
   ```

2. **Recuento de ítems en DynamoDB**

   ```bash
   aws dynamodb scan \
     --table-name coordinates \
     --select "COUNT" \
     --region us-east-1
   ```

3. **CloudWatch Logs** para cada Lambda:

   * `/aws/lambda/<stack>-IngestFunction-<id>`
   * `/aws/lambda/<stack>-ProcessFunction-<id>`

---

## Problemas Conocidos y Recomendaciones

> **PROBLEMA:** Después de cargar el CSV, **no aparece ningún mensaje** en `CoordinatesQueue` y **no hay ítems** en DynamoDB. Los CloudWatch Logs para `IngestFunction` muestran:

```
[IngestFunction][Error] Al enviar a SQS: 
An error occurred (AccessDenied) when calling the SendMessage operation:
User: arn:aws:sts::<acct-id>:assumed-role/<stack>-IngestFunctionRole-XXXX/<IngestFunctionName> 
is not authorized to perform: sqs:SendMessage 
on resource: arn:aws:sqs:us-east-1:<acct-id>:<CoordinatesQueueName> 
because no identity-based policy allows the sqs:SendMessage action
```

* **Causa raíz:** El rol `IngestFunctionRole` no tiene una política válida `iam:Allow` para enviar mensajes a la cola SQS recién creada. Aunque la plantilla SAM incluyó `- SQSSendMessagePolicy: QueueName: !Ref CoordinatesQueue`, SAM parece haber fallado al adjuntarla (o la discrepancia en el nombre lógico provocó que se adjuntara a un ARN inexistente).

* **Solución / Recomendación:**

  1. **Adjuntar manualmente** una política inline al rol `mopr3-IngestFunctionRole-<random>` en la Consola de AWS:

     ```json
     {
       "Version": "2012-10-17",
       "Statement": [
         {
           "Effect": "Allow",
           "Action": "sqs:SendMessage",
           "Resource": "arn:aws:sqs:us-east-1:<acct-id>:<CoordinatesQueueName>"
         }
       ]
     }
     ```
  2. **Reconstruir** y **volver a desplegar** la plantilla SAM después de corregir la referencia de la política (por ejemplo, referenciar explícitamente `${CoordinatesQueue.Arn}` en lugar de `QueueName`). Por ejemplo:

     ```yaml
     IngestFunction:
       Type: AWS::Serverless::Function
       Properties:
         ...
         Policies:
           - Statement:
               - Effect: Allow
                 Action:
                   - sqs:SendMessage
                 Resource: !GetAtt CoordinatesQueue.Arn
     ```
  3. **Verificar** en CloudWatch Logs que `IngestFunction` ahora pueda enviar correctamente a SQS.

* **Limitación de tasa en `postcodes.io`:**

  * La API pública tiene límite de 2.500 solicitudes/minuto según la documentación. Para \~1 M de filas:

    * Debes hacer throttling en `ProcessFunction` (por ejemplo, usar `asyncio` + semáforo o insertar una pequeña espera).
    * Alternativamente, podrías agrupar varias coordenadas por llamada a la API (`batch lookup`) si el servicio lo admite.
  * **Recomendación:** Introducir un caché intermedio (Elasticache Redis o en memoria) para evitar duplicados de búsquedas idénticas (poco probable con coordenadas aleatorias) o implementar backoff exponencial ante respuestas HTTP 429.

* **Rendimiento y Costo de DynamoDB:**

  * Elegimos el modo On-Demand (`PAY_PER_REQUEST`). Para \~1 M de escrituras, el costo es modesto pero la capacidad de escritura escala automáticamente.
  * **Recomendación:** Si la carga de trabajo se estabiliza, cambia a Modo Provisionado con Auto Scaling para reducir costos a largo plazo.

* **Validación de CSV y Reporte de Errores:**

  * La `IngestFunction` actual solo registra líneas inválidas en CloudWatch. Podrías:

    * Acumular errores en una tabla DynamoDB separada (`CoordinatesErrors`) o en una carpeta “errores” en S3.
    * Devolverle un resumen al usuario (por correo, callback de API, SNS, etc.) si hay muchas filas malformadas.

---

## Pruebas y Depuración

1. **Verificar Permisos IAM**

   * El rol de ejecución de `IngestFunction` debe tener `sqs:SendMessage` en el ARN de la cola.
   * El rol de `ProcessFunction` debe tener `dynamodb:PutItem` y `dynamodb:UpdateItem` en `CoordinatesTable`.

2. **Cargar un CSV de Prueba Pequeño**

   * Crea un `test.csv` pequeño con un encabezado y unas pocas líneas:

     ```
     id,latitude,longitude
     1,51.501009,-0.141588
     2,52.205337,0.121817
     ```
   * Solicita una URL pre-firmada:

     ```bash
     curl -s -X POST "$API_URL/presign" \
       -H "Content-Type: application/json" \
       -d '{"key":"test.csv"}' | jq -r .url
     ```
   * Carga el CSV pequeño:

     ```bash
     curl -X PUT "<presign-url>" \
       -H "Content-Type: text/csv" \
       --upload-file "./test.csv"
     ```
   * Revisa los logs de CloudWatch de `IngestFunction` y `ProcessFunction`.
   * Confirma nuevos ítems en DynamoDB:

     ```bash
     aws dynamodb scan --table-name coordinates --region us-east-1
     ```

3. **Monitorear la Profundidad de la Cola SQS**

   ```bash
   aws sqs get-queue-attributes \
     --queue-url https://sqs.us-east-1.amazonaws.com/<acct-id>/<CoordinatesQueueName> \
     --attribute-names ApproximateNumberOfMessages \
     --region us-east-1
   ```

   * Si es > 0, significa que hay mensajes esperando por procesar.

4. **Revisar Logs de Lambda**

   * **IngestFunction:**

     ```bash
     aws logs describe-log-groups \
       --region us-east-1 \
       --query "logGroups[?contains(logGroupName, 'mopr3-IngestFunction')].logGroupName"
     ```

     ```bash
     aws logs filter-log-events \
       --log-group-name "/aws/lambda/mopr3-IngestFunction-<id>" \
       --start-time $(($(date +%s) * 1000 - 300000)) \
       --region us-east-1 \
       --limit 50
     ```
   * **ProcessFunction:**

     ```bash
     aws logs describe-log-groups \
       --region us-east-1 \
       --query "logGroups[?contains(logGroupName, 'mopr3-ProcessFunction')].logGroupName"
     ```

     Si no existe ningún grupo de logs, `ProcessFunction` nunca se ha ejecutado; probablemente porque `CoordinatesQueue` no recibió mensajes.

---

## Flujo de Trabajo con Git

* Seguimos un **workflow de ramas de funcionalidad** similar a **Git Flow (simplificado)**:

  1. Todo el trabajo parte de la rama `main` (producción).
  2. Se crean ramas de funcionalidad a partir de `main`:

     * `feature/initial-architecture` (plantilla inicial `template.yaml`, Lambdas mínimas)
     * `feature/presign-function` (desarrollo de `PresignFunction`, pruebas de URL pre-firmada)
     * `feature/ingest-function` (adición de `IngestFunction`, cola SQS, políticas IAM)
     * `feature/process-function` (adición de `ProcessFunction`, interacciones con DynamoDB, llamadas a `postcodes.io`)
     * `feature/ci-cd` (configuración de GitHub Actions para linting y pruebas unitarias en Python de cada Lambda)
  3. Cada rama de funcionalidad se fusiona (“merge”) en `main` una vez validada en un entorno de desarrollo.
  4. Las fusiones siempre pasan por revisión de código y se ejecutan pruebas unitarias en cada Pull Request.

---

## Recomendaciones Adicionales

1. **Pruebas Unitarias & CI/CD**

   * Cada carpeta de Lambda incluye pruebas básicas con `pytest` en una subcarpeta `tests/` (por ejemplo, validar que el parseo de payloads de `postcodes.io` funcione, que las líneas CSV se analicen correctamente y que el formato de mensaje SQS sea válido).
   * Un workflow de GitHub Actions (`.github/workflows/ci.yml`) instala dependencias, ejecuta `pytest` y corre `sam validate`.

2. **Pruebas Locales con Docker**

   * Puedes ejecutar cada Lambda de forma local con SAM:

     ```bash
     sam local invoke PresignFunction --event events/presign-event.json
     sam local invoke IngestFunction --event events/ingest-s3-put-event.json
     sam local invoke ProcessFunction --event events/sqs-message.json
     ```
   * Usa `sam local start-api` para levantar un API Gateway local y probar el endpoint `/presign`.

3. **Pruebas de Carga & Escalado**

   * Para > 1 M de búsquedas de coordenadas, considera:

     * **Agrupar** mensajes SQS: enviar 10 coordenadas a la vez y que `ProcessFunction` recorra internamente cada lote para reducir llamadas a SQS.
     * Configurar **Concurrencia Reservada** para `ProcessFunction` y así limitar las llamadas concurrentes a `postcodes.io`.
     * Usar **DynamoDB BatchWriteItem** para reducir costos por escritura (hasta 25 ítems por lote).

4. **Seguridad**

   * Restringe tu bucket S3 para que solo el rol de `PresignFunction` pueda generar URLs PUT.
   * Habilita **Encriptación en el Lado del Servidor** en el bucket S3 (SSE-S3 o SSE-KMS).
   * Habilita **Encriptación en Reposo** en DynamoDB (clave KMS administrada por AWS por defecto).
   * Usa un **Endpoint VPC** para SQS y DynamoDB y mantener el tráfico dentro de AWS (opcional).

---

## Conclusión

Este repositorio demuestra una canalización totalmente sin servidor y basada en eventos en AWS:

* **PresignFunction** → genera URLs seguras de carga para S3.
* **IngestFunction** → analiza CSVs grandes y encola registros de coordenadas en SQS.
* **ProcessFunction** → enriquece cada coordenada vía `postcodes.io` y persiste en DynamoDB.

Aunque la configuración actual de IAM impide que `IngestFunction` envíe mensajes a SQS, la solución es simplemente corregir la política `SQSSendMessagePolicy` en la plantilla SAM (o adjuntar una política inline manualmente). Una vez resuelto, cargar cualquier CSV válido (pequeño o grande) poblará automáticamente DynamoDB con registros de coordenada + código postal.

---

> **Repositorio de GitHub:** [https://github.com/jomaldonadob/MOPR3]
> **Contacto:** [jomaldonadob@unal.edu.co]
