#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["OMP_NUM_THREADS"] = "1"

from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch
import torch.nn.functional as F
from neo4j import GraphDatabase
from dotenv import load_dotenv
from tqdm import tqdm
import multiprocessing as mp
import warnings
import time

# Configuration
load_dotenv()

MODEL_NAME = "cardiffnlp/twitter-roberta-base-sentiment"
labels = ['negative', 'neutral', 'positive']


BATCH_SIZE = 64
NUM_WORKERS = 6
# Variables pour chaque process
model = None
tokenizer = None

def init_worker():
    global model, tokenizer
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
    model.eval()

def process_batch(batch):
    global model, tokenizer
    try:
        texts = [msg for msg in batch if msg and msg.strip()]
        if not texts:
            return [{'label': 'neutral', 'score': 0.0} for _ in batch]

        inputs = tokenizer(texts, return_tensors="pt", truncation=True, padding=True, max_length=128)

        with torch.inference_mode():
            outputs = model(**inputs)
            probs = F.softmax(outputs.logits, dim=1)

        return [{
            'label': labels[torch.argmax(p)],
            'score': torch.max(p).item()
        } for p in probs]

    except Exception as e:
        return [{'label': 'neutral', 'score': 0.0, 'error': str(e)} for _ in batch]

def init_db():
    return GraphDatabase.driver(
        os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "12345678")))

def main():
    driver = init_db()

    # 1. Comptage initial
    with driver.session() as session:
        total = session.run(
            "MATCH ()-[s:SENT]->() WHERE s.sentiment IS NULL RETURN count(s)"
        ).single()[0]

        if total == 0:
            print("Aucun message à traiter")
            return

    with tqdm(total=total, unit='it',
          bar_format='{l_bar}{bar} | {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {postfix}]') as pbar, mp.Pool(NUM_WORKERS, initializer=init_worker) as pool:
        while True:
            try:
                with driver.session() as session:
                    records = session.run(
                        "MATCH ()-[s:SENT]->() "
                        "WHERE s.sentiment IS NULL "
                        "RETURN elementId(s) as id, s.message as message "
                        "LIMIT $limit", limit=BATCH_SIZE
                    ).data()

                # Afficher le nombre de messages récupérés
                print(f"Messages récupérés: {len(records)}")

                if not records:
                    break

            except Exception as e:
                print(f"Erreur lors de la récupération des messages : {e}")
                break

            start_time = time.time()  # ⏱️ Démarre le chrono avant le traitement

            messages = [r['message'] for r in records]

            # Diviser les messages en sous-batches pour les workers
            chunk_size = max(1, len(messages) // NUM_WORKERS)
            message_chunks = [messages[i:i+chunk_size] for i in range(0, len(messages), chunk_size)]

            results_chunks = pool.map(process_batch, message_chunks)
            results = [item for sublist in results_chunks for item in sublist]

            updates = [{
                'id': records[i]['id'],
                **results[i]
            } for i in range(len(records))]

            with driver.session() as session:
                session.run(
                    "UNWIND $updates AS u "
                    "MATCH ()-[s]->() WHERE elementId(s) = u.id "
                    "SET s.sentiment = u.label, s.sentiment_score = u.score",
                    updates=updates
                )

            elapsed = time.time() - start_time  # ⏱️ Temps mis pour traiter ce batch
            time_per_it = elapsed / len(records) if records else 0
            pbar.set_postfix_str(f"{time_per_it:.2f} s/it")
            pbar.update(len(records))


    driver.close()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nArrêt demandé...")
    finally:
        print("Traitement terminé")
