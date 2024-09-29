import os
import xml.etree.ElementTree as ET
from flask import (
    Flask, request, render_template, redirect, url_for, flash, session, Response
)
from flask_cors import CORS
from pymongo import MongoClient
from dotenv import load_dotenv
from groq import Groq
import logging
from datetime import datetime
import uuid
import re  # For language extraction

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)  # Set to INFO or DEBUG as needed
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Enable CORS if needed
CORS(app)

# MongoDB setup
mongo_uri = os.getenv("MONGO_URI")
try:
    client = MongoClient(mongo_uri)
    db = client['tax_ai_database']  # Use your actual database name
    conversations_collection = db['conversations']  # Collection for saving conversations
    pcc3_collection = db['pcc3_forms']  # Collection for saving PCC-3 forms
    logger.info("Connected to MongoDB successfully.")
except Exception as e:
    logger.exception("Failed to connect to MongoDB.")
    raise e

# Groq AI setup
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
try:
    client_groq = Groq(api_key=GROQ_API_KEY)
    logger.info("Groq AI client initialized successfully.")
except Exception as e:
    logger.exception("Failed to initialize Groq AI client.")
    raise e

# Context Processor to inject 'now' into all templates
@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

# Function to save conversations to MongoDB
def save_conversation(conversation_id, user_message, assistant_message):
    try:
        conversations_collection.insert_one({
            "conversation_id": conversation_id,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "timestamp": datetime.utcnow()
        })
        logger.info("Conversation saved to MongoDB.")
    except Exception as e:
        logger.exception("Failed to save conversation to MongoDB.")

# Function to send tax-related questions to Groq AI
def send_tax_question(conversation_history, language='Polish'):
    """
    Sends the conversation history to the Groq API in the specified language.
    """
    try:
        # Format the conversation history into messages
        messages = []
        for msg in conversation_history:
            role = msg.get('role')
            content = msg.get('content')
            messages.append({"role": role, "content": content})
        
        # Construct the system prompt with language specification and restrictions
        system_prompt = (
            f"You are a tax expert specializing in PCC-3 form transactions. Please respond concisely in {language}. "
            f"Only answer questions related to tax matters. If a user asks about non-tax topics, reply briefly by stating that you can only assist with tax-related queries."
        )
        messages.insert(0, {"role": "system", "content": system_prompt})

        chat_completion = client_groq.chat.completions.create(
            messages=messages,
            model="llama3-8b-8192",
            max_tokens=150,  # Adjust as per API capabilities
        )
        response = chat_completion.choices[0].message.content.strip()
        return response
    except Exception as e:
        logger.exception(f"An error occurred while communicating with Groq AI: {e}")
        return None

# Route: Home Page
@app.route('/')
def index():
    return render_template('index.html')
# Route: Generate PCC-3 Form
@app.route('/generate_pcc3', methods=['GET', 'POST'])
def generate_pcc3():
    if request.method == 'POST':
        data = request.form.to_dict()
        required_fields = [
            "pesel", "name", "dob", "region", "city",
            "street", "house_number", "postal_code",
            "date_of_transaction", "description",
            "tax_base", "tax_rate"
        ]

        # Check for missing fields
        missing_fields = [field for field in required_fields if not data.get(field)]
        if missing_fields:
            flash(f"Brakuje pól: {', '.join(missing_fields)}", "danger")
            return redirect(url_for('generate_pcc3'))

        try:
            # Validate numeric fields
            try:
                tax_base = float(data.get('tax_base'))
                tax_rate = float(data.get('tax_rate'))  # Use user-entered tax rate
            except ValueError:
                flash("Podstawa opodatkowania i stawka podatku muszą być liczbami.", "danger")
                return redirect(url_for('generate_pcc3'))

            # Calculate tax due
            tax_due = round(tax_base * (tax_rate / 100))
            logger.info(f"Obliczony podatek należny: {tax_due} PLN")

            # Generate PCC-3 XML form based on user input
            xml_output = create_pcc3_xml(data)
            
            # Save the conversation in MongoDB
            conversation_id = str(uuid.uuid4())
            user_message = f"Generowanie PCC-3 dla {data.get('name')}"
            assistant_message = f"Podatek należny: {tax_due} PLN"
            save_conversation(conversation_id, user_message, assistant_message)

            # Save the XML to MongoDB
            pcc3_collection.insert_one({
                "conversation_id": conversation_id,
                "xml_content": xml_output,
                "tax_due": tax_due,
                "timestamp": datetime.utcnow()
            })
            logger.info("PCC-3 XML zapisany w MongoDB.")

            flash("Formularz PCC-3 został wygenerowany i zapisany pomyślnie.", "success")
            return render_template('generate_pcc3.html', xml_output=xml_output, tax_due=tax_due, conversation_id=conversation_id)
        except Exception as e:
            logger.exception("Błąd podczas generowania formularza PCC-3.")
            flash(f"Nie udało się wygenerować formularza PCC-3. Błąd: {str(e)}", "danger")
            return redirect(url_for('generate_pcc3'))

    return render_template('generate_pcc3.html')


# Route: Download Generated PCC-3 XML (from MongoDB)
@app.route('/download_xml/<conversation_id>')
def download_xml(conversation_id):
    try:
        logger.debug(f"Próba pobrania XML dla conversation_id: {conversation_id}")
        
        # Retrieve the PCC-3 form from MongoDB using conversation_id
        form = pcc3_collection.find_one({"conversation_id": conversation_id})
        if not form:
            logger.warning(f"Nie znaleziono formularza PCC-3 dla conversation_id: {conversation_id}")
            flash("Formularz XML nie został znaleziony.", "danger")
            return redirect(url_for('generate_pcc3'))

        xml_content = form.get('xml_content')
        if not xml_content:
            logger.warning(f"Brak zawartości XML w formularzu dla conversation_id: {conversation_id}")
            flash("Zawartość XML jest niekompletna.", "danger")
            return redirect(url_for('generate_pcc3'))

        filename = f"PCC3_{conversation_id}.xml"
        logger.debug(f"Przygotowanie do wysłania pliku XML: {filename}")

        # Create a response with the XML content as an attachment
        response = Response(
            xml_content,
            mimetype='application/xml',
            headers={
                "Content-disposition": f"attachment; filename={filename}"
            }
        )
        logger.info(f"Wysyłanie pliku XML {filename} do użytkownika.")
        return response
    except Exception as e:
        logger.exception("Nie udało się pobrać pliku XML.")
        flash(f"Nie udało się pobrać pliku XML. Błąd: {str(e)}", "danger")
        return redirect(url_for('generate_pcc3'))

# Route: Ask AI Tax Assistant
@app.route('/ask_ai', methods=['GET', 'POST'])
def ask_ai():
    if 'conversations' not in session:
        session['conversations'] = []  # Initialize as a list to store messages

    if request.method == 'POST':
        question = request.form.get('question')
        language = request.form.get('language', 'Polish')  # Default to 'Polish'

        if not question:
            flash("Proszę wprowadzić pytanie.", "danger")
            return redirect(url_for('ask_ai'))

        logger.info(f"Otrzymano pytanie: {question} w języku: {language}")

        try:
            # Check if the language has changed; if so, reset the conversation
            if len(session['conversations']) > 0:
                last_language = None
                # Assuming the system prompt is always the first message
                first_message = session['conversations'][0]
                if first_message.get('role') == 'system':
                    # Extract language from the system prompt
                    match = re.search(r"in (\w+)", first_message.get('content'))
                    if match:
                        last_language = match.group(1)
                if last_language and last_language != language:
                    session['conversations'] = []  # Reset conversation history
                    flash("Zmieniono język rozmowy. Historia konwersacji została zresetowana.", "info")

            # Retrieve existing conversation history
            conversation_history = session['conversations']

            # Append the new user message to the history
            conversation_history.append({"role": "user", "content": question})

            # Send the entire conversation history to Groq AI
            assistant_message = send_tax_question(conversation_history, language)

            if assistant_message is None:
                flash("Nie udało się uzyskać odpowiedzi od AI. Spróbuj ponownie później.", "danger")
                return redirect(url_for('ask_ai'))

            logger.info("Otrzymano odpowiedź od Groq AI.")

            # Append the assistant's response to the history
            conversation_history.append({"role": "assistant", "content": assistant_message})

            # Update the session
            session['conversations'] = conversation_history
            session.modified = True  # To ensure session is saved

            flash("Odpowiedź AI została otrzymana i zapisana.", "success")
            return redirect(url_for('ask_ai'))
        except Exception as e:
            logger.exception("Błąd podczas komunikacji z Groq AI.")
            flash(f"Nie udało się uzyskać odpowiedzi od AI. Błąd: {str(e)}", "danger")
            return redirect(url_for('ask_ai'))

    return render_template('ask_ai.html', conversations=session.get('conversations', []))

# Route: View Specific Conversation (Optional if using continuous chat)
@app.route('/conversation/<conversation_id>')
def view_conversation(conversation_id):
    conversations = session.get('conversations', [])
    # For continuous chat, individual conversation view might not be necessary
    # This route can be repurposed or removed
    return redirect(url_for('ask_ai'))

# Helper Function: Generate PCC-3 XML
def create_pcc3_xml(data):
    try:
        logger.info("Rozpoczynanie generowania XML.")
        root = ET.Element("Deklaracja", xmlns="http://crd.gov.pl/wzor/2023/12/13/13064/")

        # Naglowek (Header)
        naglowek = ET.SubElement(root, "Naglowek")
        kod_formularza = ET.SubElement(naglowek, "KodFormularza")
        kod_formularza.text = "PCC-3"
        wariant_formularza = ET.SubElement(naglowek, "WariantFormularza")
        wariant_formularza.text = "6"
        cel_zlozenia = ET.SubElement(naglowek, "CelZlozenia", poz="P_6")
        cel_zlozenia.text = "1"  # Filing tax return
        data_transakcji = ET.SubElement(naglowek, "Data", poz="P_4")
        data_transakcji.text = data.get("date_of_transaction")

        # Podmiot (Taxpayer details)
        podmiot = ET.SubElement(root, "Podmiot1", rola="Podatnik")
        osoba_fizyczna = ET.SubElement(podmiot, "OsobaFizyczna")
        pesel = ET.SubElement(osoba_fizyczna, "PESEL")
        pesel.text = data.get("pesel")

        # Handle the case where the user provides a single or multiple names
        name_parts = data.get("name").split()
        if len(name_parts) < 2:
            raise ValueError("Proszę podać imię i nazwisko.")
        else:
            imie = ET.SubElement(osoba_fizyczna, "ImiePierwsze")
            imie.text = name_parts[0]
            nazwisko = ET.SubElement(osoba_fizyczna, "Nazwisko")
            nazwisko.text = " ".join(name_parts[1:])  # Support multi-part last names

        data_urodzenia = ET.SubElement(osoba_fizyczna, "DataUrodzenia")
        data_urodzenia.text = data.get("dob")

        # AdresZamieszkania
        adres = ET.SubElement(podmiot, "AdresZamieszkaniaSiedziby", rodzajAdresu="RAD")
        adres_pol = ET.SubElement(adres, "AdresPol")
        kraj = ET.SubElement(adres_pol, "KodKraju")
        kraj.text = "PL"
        wojewodztwo = ET.SubElement(adres_pol, "Wojewodztwo")
        wojewodztwo.text = data.get("region")
        miejscowosc = ET.SubElement(adres_pol, "Miejscowosc")
        miejscowosc.text = data.get("city")
        ulica = ET.SubElement(adres_pol, "Ulica")
        ulica.text = data.get("street")
        nr_domu = ET.SubElement(adres_pol, "NrDomu")
        nr_domu.text = data.get("house_number")
        kod_pocztowy = ET.SubElement(adres_pol, "KodPocztowy")
        kod_pocztowy.text = data.get("postal_code")

        # Transaction details (Sales contract)
        transaction = ET.SubElement(root, "PozycjeSzczegolowe")
        object_of_taxation = ET.SubElement(transaction, "P_20")
        object_of_taxation.text = "1"  # Contract
        location_of_item = ET.SubElement(transaction, "P_21")
        location_of_item.text = "1"  # Territory of Poland
        concise_description = ET.SubElement(transaction, "P_23")
        concise_description.text = data.get("description")

        # Calculation of tax
        tax_base = ET.SubElement(transaction, "P_26")
        tax_base.text = f"{float(data.get('tax_base')):.2f}"
        tax_rate = ET.SubElement(transaction, "P_27")
        tax_rate.text = f"{float(data.get('tax_rate')):.2f}"
        tax_due = ET.SubElement(transaction, "P_46")
        tax_due_value = round(float(data.get("tax_base")) * (float(data.get("tax_rate")) / 100))
        tax_due.text = str(tax_due_value)

        # Pouczenia (Caution)
        pouczenia = ET.SubElement(root, "Pouczenia")
        pouczenia.text = "1"  # Confirm acceptance of the caution

        logger.info("Generowanie XML zakończone pomyślnie.")
        return ET.tostring(root, encoding='unicode')

    except Exception as e:
        logger.exception("Nie udało się utworzyć XML PCC-3.")
        raise e

# Route: Test MongoDB Connection (Optional - for debugging)
@app.route('/test_mongo')
def test_mongo():
    try:
        test_doc = {"test_field": "test_value", "timestamp": datetime.utcnow()}
        conversations_collection.insert_one(test_doc)
        return "Zapis w MongoDB zakończony pomyślnie!"
    except Exception as e:
        logger.exception("Zapis do MongoDB nie powiódł się.")
        return f"Zapis do MongoDB nie powiódł się: {str(e)}"

# Error Handler for Unhandled Exceptions
@app.errorhandler(Exception)
def handle_exception(e):
    logger.exception("Wystąpił nieoczekiwany błąd.")
    flash(f"Wystąpił nieoczekiwany błąd: {str(e)}", "danger")
    return redirect(url_for('index'))

# Run the Flask app
if __name__ == '__main__':
    app.run(debug=True)
